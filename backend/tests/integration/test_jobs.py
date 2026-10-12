"""T5: the four expensive routes are polled jobs.

The seam is the HTTP API (submit -> 202 + job id, poll GET /jobs/{id}, read
GET /jobs/{id}/result), plus EAGER invocation of the job runner below it --
the same split the stage tests use: no Celery worker runs here, so the test
plays the worker by calling `execute_job` with the fakes the route would have
been served. Nothing here talks to Azure.
"""

import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.platform.config import get_settings
from tests.conftest import TEST_SIGNING_SECRET
from tests.integration.conftest import INVITEE_PASSWORD, authenticate, job_services

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine):
    from fdi.platform.db.engine import make_session_factory

    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client):
    """The APP's bucket (`settings.s3_bucket`'s default), so what the eager
    runner writes and what the app reads are the same objects."""
    from fdi.platform.storage.artifact_store import ArtifactStore

    s = ArtifactStore(minio_client, bucket="artifacts")
    await s.ensure_bucket()
    return s


async def _new_invitee(session_factory) -> tuple[str, str]:
    from fdi.platform.db.repository import UserRepository
    from fdi.platform.security import hash_password

    email = f"invitee-{uuid.uuid4().hex}@example.test"
    async with session_factory() as s:
        await UserRepository(s).create(email=email, password_hash=hash_password(INVITEE_PASSWORD))
        await s.commit()
    return email, INVITEE_PASSWORD


@pytest.fixture
async def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch, invitee):
    """An app wired to the real containers and logged in as `creds` (the
    `invitee` fixture by default). Dispatch publishes to the real broker;
    nothing consumes it, so a submitted job stays `queued` until a test runs
    it eagerly."""

    @asynccontextmanager
    async def _make(*, creds=None, dependency_overrides=None):
        env = {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
            "FDI_AUTH_SECRET": TEST_SIGNING_SECRET,
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        if dependency_overrides:
            app.dependency_overrides.update(dependency_overrides)
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await authenticate(client, creds or invitee)
                yield client

    return _make


def _answering_agent(answer: str):
    from fdi.core.agent import AgentResponse
    from fdi.pipeline.agent import FakeAgentClient

    return FakeAgentClient([AgentResponse(content=answer)])


# --------------------------------------------------------------------------
# /ask
# --------------------------------------------------------------------------


async def test_ask_is_accepted_as_a_queued_job(make_client, _alembic_applied) -> None:
    from fdi.api.deps import get_agent_client

    agent = _answering_agent("unused")
    async with make_client(dependency_overrides={get_agent_client: lambda: agent}) as client:
        r = await client.post("/ask", json={"question": "What was revenue?"})
        assert r.status_code == 202, r.text
        accepted = r.json()
        assert accepted["status"] == "queued"
        assert accepted["correlation_id"]
        assert uuid.UUID(accepted["job_id"]).version == 4

        polled = await client.get(f"/jobs/{accepted['job_id']}")
    assert polled.status_code == 200
    assert polled.json()["status"] == "queued"
    assert polled.json()["kind"] == "ask"
    # The agent was never called by the request: the work is the worker's.
    assert agent.calls == []


async def test_ask_job_walks_to_succeeded_and_serves_the_inline_body(
    make_client, session_factory, store, _alembic_applied
) -> None:
    from fdi.api.deps import get_agent_client
    from fdi.pipeline.jobs import execute_job

    agent = _answering_agent("Revenue was 42.")
    async with make_client(dependency_overrides={get_agent_client: lambda: agent}) as client:
        job_id = (await client.post("/ask", json={"question": "What was revenue?"})).json()[
            "job_id"
        ]
        early = await client.get(f"/jobs/{job_id}/result")
        assert early.status_code == 409

        await execute_job(job_id, job_services(session_factory, store, agent_client=agent))

        polled = (await client.get(f"/jobs/{job_id}")).json()
        result = await client.get(f"/jobs/{job_id}/result")
    assert polled["status"] == "succeeded"
    assert polled["error"] is None
    assert result.status_code == 200
    # The shape /ask used to return inline (AskResponse).
    assert result.json() == {"answer": "Revenue was 42.", "citations": [], "tool_trace": []}
    assert len(agent.calls) == 1


async def test_a_crashing_job_fails_with_its_error_and_correlation_id(
    make_client, session_factory, store, _alembic_applied
) -> None:
    from fdi.api.deps import get_agent_client
    from fdi.pipeline.agent import FakeAgentClient
    from fdi.pipeline.jobs import execute_job

    exhausted = FakeAgentClient([])  # raises AssertionError on its first call
    async with make_client(
        dependency_overrides={get_agent_client: lambda: _answering_agent("unused")}
    ) as client:
        accepted = (await client.post("/ask", json={"question": "q"})).json()
        # The crash still propagates -- Celery and Sentry must see it.
        with pytest.raises(AssertionError):
            await execute_job(
                accepted["job_id"], job_services(session_factory, store, agent_client=exhausted)
            )
        polled = (await client.get(f"/jobs/{accepted['job_id']}")).json()
        result = await client.get(f"/jobs/{accepted['job_id']}/result")
    assert polled["status"] == "failed"
    assert "FakeAgentClient exhausted" in polled["error"]
    assert polled["correlation_id"] == accepted["correlation_id"]
    assert result.status_code == 409


async def test_a_job_the_worker_cannot_answer_fails_without_crashing(
    make_client, session_factory, store, _alembic_applied
) -> None:
    """The API had an agent client; the worker does not. That is a readable
    failure on the job, not an exception escaping the runner."""
    from fdi.api.deps import get_agent_client
    from fdi.pipeline.jobs import execute_job

    async with make_client(
        dependency_overrides={get_agent_client: lambda: _answering_agent("unused")}
    ) as client:
        job_id = (await client.post("/ask", json={"question": "q"})).json()["job_id"]
        await execute_job(job_id, job_services(session_factory, store, agent_client=None))
        polled = (await client.get(f"/jobs/{job_id}")).json()
    assert polled["status"] == "failed"
    assert "agent_client" in polled["error"]


async def test_jobs_are_invisible_to_other_invitees(
    make_client, session_factory, _alembic_applied
) -> None:
    from fdi.api.deps import get_agent_client

    overrides = {get_agent_client: lambda: _answering_agent("unused")}
    async with make_client(dependency_overrides=overrides) as owner:
        job_id = (await owner.post("/ask", json={"question": "q"})).json()["job_id"]
        mine = (await owner.get("/jobs")).json()["jobs"]
        asks = (await owner.get("/jobs?kind=ask")).json()["jobs"]
        assert (await owner.get("/jobs?kind=extact")).status_code == 422
    assert job_id in [j["job_id"] for j in mine]
    assert job_id in [j["job_id"] for j in asks]

    stranger = await _new_invitee(session_factory)
    async with make_client(creds=stranger, dependency_overrides=overrides) as other:
        assert (await other.get(f"/jobs/{job_id}")).status_code == 404
        assert (await other.get(f"/jobs/{job_id}/result")).status_code == 404
        assert (await other.get("/jobs")).json()["jobs"] == []


# --------------------------------------------------------------------------
# /eval
# --------------------------------------------------------------------------


async def test_eval_job_is_running_while_it_works_then_succeeds(
    make_client, session_factory, store, _alembic_applied
) -> None:
    """`running` observed through the HTTP seam: the store the job reads its
    dataset from polls the job before answering."""
    from fdi.pipeline.candidates.uppercase import toy_dataset
    from fdi.pipeline.dataset_store import put_dataset
    from fdi.pipeline.jobs import execute_job
    from fdi.platform.storage.artifact_store import ArtifactStore

    await put_dataset(store, toy_dataset())
    seen: list[str] = []

    async with make_client() as client:
        r = await client.post(
            "/eval", json={"candidate": "uppercase", "dataset": "toy", "dataset_version": 1}
        )
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]

        class PollingStore(ArtifactStore):
            async def get_json(self, key: str):  # type: ignore[override]
                seen.append((await client.get(f"/jobs/{job_id}")).json()["status"])
                return await super().get_json(key)

        spy = PollingStore(store._client, bucket=store._bucket)
        await execute_job(job_id, job_services(session_factory, spy))
        polled = (await client.get(f"/jobs/{job_id}")).json()
        result = (await client.get(f"/jobs/{job_id}/result")).json()

    assert seen == ["running"]
    assert polled["status"] == "succeeded"
    assert result["aggregate"] == 1.0
    assert uuid.UUID(result["run_id"]).version == 4

