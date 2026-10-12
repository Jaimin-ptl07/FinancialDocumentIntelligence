"""T7: per-user rate limits, observed through the HTTP API against real Redis.

Two meters, both keyed by the Invitee:
  - a daily quota on expensive job submissions and on uploads (a fixed window,
    Redis counters), and
  - a concurrency cap on expensive jobs still in flight (queued or running).

Every refusal is a 429 problem+json naming its `reason`. Quota state rides
every metered response as `RateLimit-Limit` / `RateLimit-Remaining` /
`RateLimit-Reset` headers. The limits here are set through the same `FDI_`
settings an operator would use, shrunk so a test exhausts them in a few calls.
"""

import asyncio
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


def _agent():
    from fdi.core.agent import AgentResponse
    from fdi.pipeline.agent import FakeAgentClient

    return FakeAgentClient([AgentResponse(content="42.")])


@pytest.fixture
async def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch, invitee):
    """An app on the real containers, logged in as `creds`, with `/ask`
    served by a fake agent and the limits given as FDI_ settings. Nothing
    consumes the broker, so a submitted job stays `queued` -- in flight --
    until a test runs it eagerly."""

    @asynccontextmanager
    async def _make(*, creds=None, **limits: int):
        from fdi.api.deps import get_agent_client

        env = {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
            "FDI_AUTH_SECRET": TEST_SIGNING_SECRET,
        }
        env.update({f"FDI_RATE_LIMIT_{k.upper()}": str(v) for k, v in limits.items()})
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        agent = _agent()
        app.dependency_overrides[get_agent_client] = lambda: agent
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await authenticate(client, creds or invitee)
                yield client

    return _make


async def _ask(client: AsyncClient):
    return await client.post("/ask", json={"question": "What was revenue?"})


async def _upload(client: AsyncClient):
    pdf = f"PDF-RL-{uuid.uuid4()}".encode()
    return await client.post(
        "/documents", files={"file": ("a.pdf", pdf, "application/pdf")}
    )


def _assert_problem_429(r, reason: str) -> dict:
    assert r.status_code == 429, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 429
    assert body["title"] == "Too Many Requests"
    assert body["reason"] == reason
    assert body["detail"]
    return body


# --------------------------------------------------------------------------
# Daily quota: expensive jobs
# --------------------------------------------------------------------------


async def test_job_quota_exhaustion_returns_429_problem(make_client, _alembic_applied) -> None:
    async with make_client(jobs_per_day=2, max_concurrent_jobs=10) as client:
        first = await _ask(client)
        second = await _ask(client)
        third = await _ask(client)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    body = _assert_problem_429(third, "daily_quota")
    assert body["limit"] == 2
    assert body["remaining"] == 0
    assert int(third.headers["Retry-After"]) >= 1


async def test_quota_state_is_observable_in_headers(make_client, _alembic_applied) -> None:
    async with make_client(jobs_per_day=3, max_concurrent_jobs=10) as client:
        first = await _ask(client)
        second = await _ask(client)

    assert first.headers["RateLimit-Limit"] == "3"
    assert first.headers["RateLimit-Remaining"] == "2"
    assert second.headers["RateLimit-Remaining"] == "1"
    # Seconds until the window resets: positive, never more than one day.
    assert 1 <= int(first.headers["RateLimit-Reset"]) <= 86_400


async def test_every_expensive_route_draws_on_the_same_job_quota(
    make_client, _alembic_applied
) -> None:
    """The quota is per Invitee, not per route: /ask spends it and /eval
    is refused. (An unknown candidate would 404 first, so a real one.)"""
    async with make_client(jobs_per_day=1, max_concurrent_jobs=10) as client:
        assert (await _ask(client)).status_code == 202
        r = await client.post(
            "/eval", json={"candidate": "uppercase", "dataset": "d", "dataset_version": 1}
        )
    _assert_problem_429(r, "daily_quota")


async def test_a_refused_submission_does_not_spend_quota(make_client, _alembic_applied) -> None:
    """A request answered 404 at submit never became a job, so it costs
    nothing -- the next real submission still fits under the cap."""
    async with make_client(jobs_per_day=1, max_concurrent_jobs=10) as client:
        missing = await client.post("/extract/document", json={"doc_key": "no-such-key"})
        assert missing.status_code in (404, 503)
        assert (await _ask(client)).status_code == 202


async def test_quota_is_per_invitee(make_client, session_factory, _alembic_applied) -> None:
    async with make_client(jobs_per_day=1, max_concurrent_jobs=10) as client:
        assert (await _ask(client)).status_code == 202
        _assert_problem_429(await _ask(client), "daily_quota")

    other = await _new_invitee(session_factory)
    async with make_client(creds=other, jobs_per_day=1, max_concurrent_jobs=10) as client:
        r = await _ask(client)
    assert r.status_code == 202, r.text


async def test_quota_window_resets(make_client, _alembic_applied) -> None:
    """A 2-second window: exhaust it, wait out the advertised reset, and the
    next submission is accepted again."""
    async with make_client(
        jobs_per_day=1, max_concurrent_jobs=10, window_seconds=2
    ) as client:
        assert (await _ask(client)).status_code == 202
        refused = await _ask(client)
        _assert_problem_429(refused, "daily_quota")

        await asyncio.sleep(int(refused.headers["Retry-After"]) + 0.2)
        again = await _ask(client)
    assert again.status_code == 202, again.text
    assert again.headers["RateLimit-Remaining"] == "0"


# --------------------------------------------------------------------------
# Concurrency: one expensive job in flight per Invitee
# --------------------------------------------------------------------------


async def test_second_job_while_one_is_in_flight_returns_429(
    make_client, _alembic_applied
) -> None:
    async with make_client(max_concurrent_jobs=1) as client:
        first = await _ask(client)
        second = await client.post(
            "/eval", json={"candidate": "uppercase", "dataset": "d", "dataset_version": 1}
        )

    assert first.status_code == 202, first.text
    body = _assert_problem_429(second, "concurrency")
    assert body["limit"] == 1
    assert body["active_job_ids"] == [first.json()["job_id"]]


async def test_a_concurrency_refusal_does_not_spend_quota(make_client, _alembic_applied) -> None:
    async with make_client(jobs_per_day=5, max_concurrent_jobs=1) as client:
        first = await _ask(client)
        _assert_problem_429(await _ask(client), "concurrency")
    assert first.headers["RateLimit-Remaining"] == "4"


async def test_a_finished_job_frees_the_slot(
    make_client, session_factory, store, _alembic_applied
) -> None:
    from fdi.pipeline.jobs import execute_job

    async with make_client(max_concurrent_jobs=1) as client:
        job_id = (await _ask(client)).json()["job_id"]
        _assert_problem_429(await _ask(client), "concurrency")

        await execute_job(job_id, job_services(session_factory, store, agent_client=_agent()))
        assert (await client.get(f"/jobs/{job_id}")).json()["status"] == "succeeded"

        r = await _ask(client)
    assert r.status_code == 202, r.text


async def test_concurrency_is_per_invitee(make_client, session_factory, _alembic_applied) -> None:
    async with make_client(max_concurrent_jobs=1) as client:
        assert (await _ask(client)).status_code == 202

    other = await _new_invitee(session_factory)
    async with make_client(creds=other, max_concurrent_jobs=1) as client:
        assert (await _ask(client)).status_code == 202


async def test_simultaneous_submissions_admit_exactly_one(make_client, _alembic_applied) -> None:
    """The cap holds under a race: parallel tabs submitting at once."""
    async with make_client(max_concurrent_jobs=1, jobs_per_day=50) as client:
        results = await asyncio.gather(*(_ask(client) for _ in range(4)))
    statuses = sorted(r.status_code for r in results)
    assert statuses == [202, 429, 429, 429], [r.text for r in results]


# --------------------------------------------------------------------------
# Daily quota: uploads
# --------------------------------------------------------------------------


async def test_upload_quota_exhaustion_returns_429_problem(make_client, _alembic_applied) -> None:
    async with make_client(uploads_per_day=2) as client:
        first = await _upload(client)
        second = await _upload(client)
        third = await _upload(client)

    assert first.status_code == 201, first.text
    assert first.headers["RateLimit-Limit"] == "2"
    assert first.headers["RateLimit-Remaining"] == "1"
    assert second.status_code == 201, second.text
    body = _assert_problem_429(third, "daily_quota")
    assert body["limit"] == 2


async def test_uploads_and_jobs_are_metered_separately(make_client, _alembic_applied) -> None:
    async with make_client(uploads_per_day=1, jobs_per_day=1, max_concurrent_jobs=10) as client:
        assert (await _upload(client)).status_code == 201
        assert (await _ask(client)).status_code == 202
        _assert_problem_429(await _upload(client), "daily_quota")


async def test_an_unacceptable_upload_does_not_spend_quota(make_client, _alembic_applied) -> None:
    async with make_client(uploads_per_day=1) as client:
        bad = await client.post(
            "/documents", files={"file": ("x.txt", b"hi", "text/plain")}
        )
        assert bad.status_code == 415
        assert (await _upload(client)).status_code == 201
