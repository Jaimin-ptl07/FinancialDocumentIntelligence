"""The HTTP surface, against live containers.

Two themes run through this file.

**`/ready` must be able to fail.** The plan's version asserted `postgres is True`
and `s3 is True` against healthy containers, which is equally true of a check
that reads `return True` -- and one of the three fields *was* `return True`.
Every backend therefore gets a test that points that backend, and only that
backend, at a dead port: a check wired to the wrong thing, or to nothing, cannot
survive all three.

**`X-Request-ID` is client-controlled input.** It reaches a varchar(36) column, a
response header, a URI, and a JSON log line, and the plan validated it nowhere.
Measured before the fix: 37 characters -> 500, 10 KB -> 500.
"""

import json
import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.platform.config import get_settings

pytestmark = pytest.mark.integration

# Port 1 is unroutable and refuses immediately -- a dead backend that costs a
# syscall, not a timeout. Same trick as conftest's _STUB_DATABASE_URL.
DEAD_POSTGRES = "postgresql+asyncpg://dead:dead@127.0.0.1:1/dead"
DEAD_S3 = "127.0.0.1:1"
DEAD_BROKER = "amqp://guest:guest@127.0.0.1:1/%2F"


@pytest.fixture
def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch):
    """Build a client whose app is wired to real containers, or to dead ports.

    A factory rather than a fixture so a single test can choose which backend is
    dead. The app is built and its lifespan entered per call, which is also what
    makes the engine-in-lifespan rule (Global Constraint 3) observable here at
    all: nothing is wired until lifespan runs.
    """

    @asynccontextmanager
    async def _make(*, raise_app_exceptions: bool = True, **overrides: str):
        env = {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
        }
        env.update(overrides)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        # ASGITransport does NOT drive the lifespan protocol -- it only ever
        # sends an `http` scope. Without this the engine, session factory and
        # artifact store are never created and every backend-touching route
        # dies on `KeyError: 'session_factory'`. The plan's fixture omitted it.
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                yield client

    return _make


@pytest.fixture
async def client(make_client):
    async with make_client() as c:
        yield c


@pytest.fixture
def session_factory(migrated_engine):
    from fdi.platform.db.engine import make_session_factory

    return make_session_factory(migrated_engine)


# --------------------------------------------------------------------------
# The plan's five.
# --------------------------------------------------------------------------


async def test_health_is_liveness_only(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_checks_backends(client: AsyncClient, _alembic_applied) -> None:
    r = await client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["postgres"] is True
    assert body["s3"] is True
    assert body["broker"] is True


async def test_post_ping_job_returns_202_with_job_id(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/ping-job")
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"
    assert len(body["job_id"]) == 36
    assert len(body["correlation_id"]) == 36
    # 36 is the uuid4 string length, but asserting the length only pins the
    # length: "a" * 36 passes it too. What both fields actually promise is a
    # uuid, and that is also what keeps them inside document.id / stage_run's
    # correlation_id varchar(36). Assert the promise, not its side effect.
    assert uuid.UUID(body["job_id"]).version == 4
    assert uuid.UUID(body["correlation_id"]).version == 4


async def test_post_ping_job_honours_inbound_request_id(
    client: AsyncClient, _alembic_applied
) -> None:
    """Propagating a caller's request id is the feature; verbatim is the point.

    A value we silently rewrite would be worse than ignoring the header: the
    caller's logs and ours would disagree about the same request while both
    looked right.
    """
    r = await client.post("/ping-job", headers={"X-Request-ID": "caller-supplied-id"})
    assert r.status_code == 202
    assert r.json()["correlation_id"] == "caller-supplied-id"


async def test_get_unknown_job_is_404_problem_json(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.get("/jobs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")


# --------------------------------------------------------------------------
# X-Request-ID is client-controlled. Every sink it reaches has a limit.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("why", "value"),
    [
        # The one that was a live 500. correlation_id is String(36) and
        # PostgreSQL RAISES StringDataRightTruncation past a varchar's width
        # rather than truncating (Task 11 measured this) -- so one extra
        # character in a client header chose the response code.
        ("37 chars: one past the varchar(36) column", "a" * 37),
        ("10 KB: unbounded header", "z" * 10240),
        # structlog's JSONRenderer escapes these correctly -- verified in
        # test_log_line_cannot_be_forged_through_the_header below -- so these
        # are rejected for the sinks that do NOT escape: a response header
        # (splitting) and the `instance` URI.
        ("embedded newline", "a\nb"),
        ("embedded CRLF", "a\r\nb"),
        ("JSON injection payload", 'x","level":"critical","evil":"'),
        ("space", "has a space"),
        ("control character", "a\x00b"),
    ],
)
async def test_unusable_request_id_is_rejected_not_repaired(
    client: AsyncClient, _alembic_applied, why: str, value: str
) -> None:
    """400, not a 500 and not a silent repair.

    Truncating or ignoring would both break the one property the header exists
    to provide -- that the caller's id and ours are the same string. The input
    is the caller's, so the caller is who should hear about it.
    """
    r = await client.post("/ping-job", headers={"X-Request-ID": value})
    assert r.status_code == 400, why
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 400
    # The rejection still carries a handle: a fresh id is minted and bound
    # before the refusal is rendered, so even a refused request is traceable.
    assert body["instance"].startswith("urn:uuid:")


async def test_rejected_request_id_is_not_echoed_back(
    client: AsyncClient, _alembic_applied
) -> None:
    """A refused id must not reappear in the response it was refused for."""
    r = await client.post("/ping-job", headers={"X-Request-ID": "a\r\nb"})
    assert r.status_code == 400
    echoed = r.headers["x-request-id"]
    assert echoed != "a\r\nb"
    assert uuid.UUID(echoed).version == 4
    assert "\n" not in json.dumps(dict(r.headers))


async def test_empty_request_id_header_mints_rather_than_rejects(
    client: AsyncClient, _alembic_applied
) -> None:
    """`X-Request-ID:` with no value is absent, not invalid.

    A bare header is a proxy artefact, not a caller asserting an id. Minting
    here is also what keeps `""` out of `stage_run.correlation_id` -- the third
    representation of "absent" (with NULL and a real id), and the only one that
    satisfies both the column and the repository's `str` signature while meaning
    nothing.
    """
    r = await client.post("/ping-job", headers={"X-Request-ID": ""})
    assert r.status_code == 202
    assert uuid.UUID(r.json()["correlation_id"]).version == 4


@pytest.mark.parametrize(
    "value",
    [
        "caller-supplied-id",
        "0194fdc2-fa2f-4cc0-81d3-ff12045b73c8",  # uuid4, dashed
        "0194fdc2fa2f4cc081d3ff12045b73c8",  # nginx $request_id: 32 undashed hex
        "1-63441c4a-abcdef012345678912345678",  # AWS X-Ray, 35 chars
        "a" * 36,  # exactly the column width
        "a",  # one character
    ],
)
async def test_request_id_formats_a_real_proxy_stamps_are_accepted(
    client: AsyncClient, _alembic_applied, value: str
) -> None:
    """The rule must not reject the fronting proxies this app will sit behind.

    This is why the id is not simply required to be a UUID, which would close
    every hazard here by construction: an ALB or nginx in front of this service
    stamps its own format, and a 400 on every request is a worse failure than
    the one being fixed.
    """
    r = await client.post("/ping-job", headers={"X-Request-ID": value})
    assert r.status_code == 202
    assert r.json()["correlation_id"] == value


async def test_accepted_request_id_reaches_the_synchronous_response_and_log(
    capsys, client: AsyncClient, _alembic_applied
) -> None:
    """The accepted id is real, not just validated; prove it survives to the
    two places that are synchronously true right after `POST /ping-job`.

    Under the stage framework, `/ping-job` only creates a `document` row --
    the row that carries a `correlation_id` column is `stage_run`, and that is
    written by the worker, which this test does not run. So the id no longer
    reaches Postgres synchronously (a document row does not carry a
    correlation id at all). What IS still synchronously true, and still worth
    pinning: the accepted `X-Request-ID` is echoed as `correlation_id` in the
    202 body, and it appears in the `pipeline.queued` log line emitted before
    the response is returned.

    `configure_logging()` here, in the test body, is load-bearing -- see
    `test_log_line_cannot_be_forged_through_the_header` below for why.
    """
    from fdi.platform.obs.logging import configure_logging

    configure_logging("INFO")
    value = "b" * 36
    r = await client.post("/ping-job", headers={"X-Request-ID": value})
    assert r.status_code == 202
    assert r.json()["correlation_id"] == value

    lines = [ln for ln in capsys.readouterr().out.strip().splitlines() if ln.startswith("{")]
    entry = next(p for p in (json.loads(ln) for ln in lines) if p.get("event") == "pipeline.queued")
    assert entry["doc_id"] == r.json()["job_id"]
    assert entry["correlation_id"] == value


async def test_log_line_cannot_be_forged_through_the_header(
    capsys, client, _alembic_applied
) -> None:
    """Pins the *reason* the charset rule is not what stops log injection.

    structlog's JSONRenderer escapes quotes and newlines, so a header of
    `x","level":"critical","evil":"` lands as an escaped string value, on one
    line, with `level` still `info`. Measured, not assumed -- and worth pinning,
    because if the renderer were ever swapped for one that interpolates, the
    charset rule alone would not save us and nothing else would notice.

    `configure_logging()` here, in the test body, is load-bearing: it binds a
    StreamHandler to `sys.stdout` *as it is now*. The `client` fixture ran
    lifespan during pytest's SETUP phase, whose CaptureIO is already closed by
    the time the body runs -- so the handler from lifespan drops every record
    with `--- Logging error --- ValueError: I/O operation on closed file`, and a
    test asserting on log output would silently read an empty string.
    """
    from fdi.platform.obs.logging import configure_logging

    configure_logging("INFO")
    accepted = "x-level-critical-evil"  # same shape, inside the charset rule
    r = await client.post("/ping-job", headers={"X-Request-ID": accepted})
    assert r.status_code == 202

    lines = [ln for ln in capsys.readouterr().out.strip().splitlines() if ln.startswith("{")]
    entry = next(p for p in (json.loads(ln) for ln in lines) if p.get("event") == "pipeline.queued")
    assert entry["correlation_id"] == accepted
    assert entry["level"] == "info"


# --------------------------------------------------------------------------
# /ready must be able to fail. One dead backend at a time.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("down", "overrides"),
    [
        ("postgres", {"FDI_DATABASE_URL": DEAD_POSTGRES}),
        ("s3", {"FDI_S3_ENDPOINT": DEAD_S3}),
        ("broker", {"FDI_BROKER_URL": DEAD_BROKER}),
    ],
)
async def test_ready_reports_the_one_dead_backend(
    make_client, _alembic_applied, down: str, overrides: dict[str, str]
) -> None:
    """Each field must track its OWN backend.

    Killing one at a time is what separates a real probe from `return True` and
    from a probe wired to the wrong backend. `broker` shipped as a literal
    `True`: it passed the plan's test, which never asserted it, and would still
    pass a test that only killed Postgres.
    """
    async with make_client(**overrides) as client:
        r = await client.get("/ready")

    assert r.status_code == 503, "a backend is down; this instance cannot serve traffic"
    body = r.json()
    assert body[down] is False, f"{down} is dead but /ready reported it healthy"
    for other in {"postgres", "s3", "broker"} - {down}:
        assert body[other] is True, f"{other} is up but /ready reported it down"


async def test_health_is_still_200_when_every_backend_is_dead(
    make_client, _alembic_applied
) -> None:
    """The whole distinction between /health and /ready, stated once.

    Liveness answers "restart this process?". A backend outage is not a reason
    to kill the API -- the orchestrator would restart-loop every replica while
    the DB recovers, turning a recoverable outage into an unrecoverable one.
    """
    async with make_client(
        FDI_DATABASE_URL=DEAD_POSTGRES, FDI_S3_ENDPOINT=DEAD_S3, FDI_BROKER_URL=DEAD_BROKER
    ) as client:
        health = await client.get("/health")
        ready = await client.get("/ready")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert ready.status_code == 503, "the same app, same moment: /ready must disagree"


# --------------------------------------------------------------------------
# RFC 9457 `instance` -- the caller's handle.
# --------------------------------------------------------------------------


async def test_problem_body_carries_the_correlation_id_as_instance(
    client: AsyncClient, _alembic_applied
) -> None:
    """Without this a 404 body is four opaque fields with nothing to quote."""
    r = await client.get(
        "/jobs/00000000-0000-0000-0000-000000000000",
        headers={"X-Request-ID": "caller-supplied-id"},
    )
    assert r.status_code == 404
    # Not urn:uuid: -- "caller-supplied-id" is not a UUID, and claiming it is
    # would be a lie a client could try to parse.
    assert r.json()["instance"] == "urn:fdi:request:caller-supplied-id"
    assert r.headers["x-request-id"] == "caller-supplied-id"


async def test_minted_correlation_id_gets_a_urn_uuid_instance(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.get("/jobs/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    instance = r.json()["instance"]
    assert instance == f"urn:uuid:{r.headers['x-request-id']}"
    assert uuid.UUID(instance.removeprefix("urn:uuid:")).version == 4


async def test_every_response_carries_the_correlation_id_header(
    client: AsyncClient, _alembic_applied
) -> None:
    """A caller who sent no id still needs to learn the one we minted."""
    for path in ("/health", "/ready"):
        r = await client.get(path)
        assert uuid.UUID(r.headers["x-request-id"]).version == 4, path


async def test_correlation_id_does_not_leak_between_requests(
    client: AsyncClient, _alembic_applied
) -> None:
    """Task 14's bug, one layer up.

    Nothing resets contextvars between requests on a reused task context, so a
    request that failed to bind would log under its predecessor's id. The
    middleware clears in a `finally` for the same reason the Celery wrapper does.
    """
    first = await client.post("/ping-job", headers={"X-Request-ID": "first-request-id"})
    second = await client.post("/ping-job")
    assert first.json()["correlation_id"] == "first-request-id"
    assert second.json()["correlation_id"] != "first-request-id"
    assert uuid.UUID(second.json()["correlation_id"]).version == 4


# --------------------------------------------------------------------------
# Task 13: staleness + reprocess.
# --------------------------------------------------------------------------


async def test_stale_reflects_recorded_runs(
    client: AsyncClient, _alembic_applied, session_factory
) -> None:
    from fdi.pipeline.keys import root_input_hash
    from fdi.platform.db.repository import RegistryRepository

    doc_id = (await client.post("/ping-job")).json()["job_id"]
    # No run recorded yet -> the root stage is stale.
    assert (await client.get(f"/docs/{doc_id}/stale")).json() == {"stale": ["ping"]}

    # Seed a completed ping run; now nothing is stale.
    rid = str(uuid.uuid4())
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.start_run(rid, doc_id, "ping", 1, root_input_hash(None), None)
        await repo.finish_run(
            rid, output_hash="oh", artifact_key=f"artifacts/{doc_id}/ping/v1/x.json"
        )
        await s.commit()
    assert (await client.get(f"/docs/{doc_id}/stale")).json() == {"stale": []}


async def test_reprocess_dispatches_from_stage(client: AsyncClient, _alembic_applied) -> None:
    doc_id = (await client.post("/ping-job")).json()["job_id"]
    r = await client.post(f"/docs/{doc_id}/reprocess?from=ping")
    assert r.status_code == 202
    assert r.json()["dispatched"] == ["ping"]


async def test_reprocess_unknown_document_is_404(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post(
        "/docs/00000000-0000-0000-0000-000000000000/reprocess?from=ping"
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Task 9: POST /eval + GET /datasets/{name}/leaderboard
# --------------------------------------------------------------------------


async def test_eval_endpoint_runs_and_ranks(
    client: AsyncClient, minio_client, _alembic_applied
) -> None:
    """Seed the toy dataset into the APP's bucket -- not the test `store` fixture's.

    `make_client` never sets `FDI_S3_BUCKET`, so the app's `ArtifactStore` opens
    `settings.s3_bucket`'s default, `"artifacts"` -- a different bucket than the
    `test-artifacts` the plain `store` fixture writes to. Seeding through that
    fixture would leave the dataset invisible to the running app and turn this
    into a 500 from `get_dataset`, not the 200 this test wants.
    """
    from fdi.pipeline.candidates.uppercase import toy_dataset
    from fdi.pipeline.dataset_store import put_dataset
    from fdi.platform.storage.artifact_store import ArtifactStore

    seed_store = ArtifactStore(minio_client, bucket="artifacts")
    await seed_store.ensure_bucket()
    await put_dataset(seed_store, toy_dataset())

    r = await client.post(
        "/eval", json={"candidate": "uppercase", "dataset": "toy", "dataset_version": 1}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["aggregate"] == 1.0
    assert uuid.UUID(body["run_id"]).version == 4

    board = (await client.get("/datasets/toy/leaderboard?metric=exact_match")).json()
    assert board["leaderboard"][0]["candidate"] == "uppercase"
    assert board["leaderboard"][0]["version"] == 1
    assert board["leaderboard"][0]["aggregate"] == 1.0


async def test_eval_endpoint_unknown_candidate_is_404(
    client: AsyncClient, minio_client, _alembic_applied
) -> None:
    from fdi.pipeline.candidates.uppercase import toy_dataset
    from fdi.pipeline.dataset_store import put_dataset
    from fdi.platform.storage.artifact_store import ArtifactStore

    seed_store = ArtifactStore(minio_client, bucket="artifacts")
    await seed_store.ensure_bucket()
    await put_dataset(seed_store, toy_dataset())

    r = await client.post(
        "/eval", json={"candidate": "no-such-candidate", "dataset": "toy", "dataset_version": 1}
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Task 6: POST /documents upload (dedup+versioning) + reads
# --------------------------------------------------------------------------


async def test_document_upload_dedup_and_versions(
    client: AsyncClient, _alembic_applied
) -> None:
    # Unique content + doc_key per test run: the migrated DB and MinIO bucket
    # are session-scoped (Global Constraint), and `UNIQUE(checksum)` is real --
    # literal bytes like b"PDF-A" would collide with test_intake.py's fixture
    # of the same name and silently dedup against ITS row instead of creating
    # this test's own.
    doc_key = f"annual-{uuid.uuid4()}"
    pdf_a = f"PDF-A-{uuid.uuid4()}".encode()
    pdf_b = f"PDF-B-{uuid.uuid4()}".encode()

    r = await client.post(
        "/documents",
        files={"file": ("a.pdf", pdf_a, "application/pdf")},
        data={"doc_key": doc_key},
    )
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["version"] == 1 and doc["checksum"]

    same = await client.post(
        "/documents",
        files={"file": ("a2.pdf", pdf_a, "application/pdf")},
        data={"doc_key": doc_key},
    )
    assert same.json()["id"] == doc["id"]  # dedup

    r2 = await client.post(
        "/documents",
        files={"file": ("b.pdf", pdf_b, "application/pdf")},
        data={"doc_key": doc_key},
    )
    assert r2.json()["version"] == 2

    got = await client.get(f"/documents/{doc['id']}")
    assert got.status_code == 200 and got.json()["filename"] == "a.pdf"
    versions = (await client.get(f"/documents?doc_key={doc_key}")).json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]


async def test_non_pdf_is_415(client: AsyncClient, _alembic_applied) -> None:
    r = await client.post(
        "/documents", files={"file": ("x.txt", b"hi", "text/plain")}
    )
    assert r.status_code == 415