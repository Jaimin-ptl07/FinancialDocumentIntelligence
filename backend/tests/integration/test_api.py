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
    async def _make(
        *,
        raise_app_exceptions: bool = True,
        dependency_overrides: dict[object, object] | None = None,
        **overrides: str,
    ):
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
        if dependency_overrides:
            app.dependency_overrides.update(dependency_overrides)
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


@pytest.fixture
async def store(minio_client):
    """Bucket `"artifacts"` -- the same bucket `make_client`'s app opens by
    default (Global Constraint: `make_client` never sets `FDI_S3_BUCKET`).

    `test_eval_endpoint_runs_and_ranks` above hit this exact pitfall with the
    OTHER convention (`ArtifactStore(minio_client, bucket="test-artifacts")`
    used by every other integration test file's `store` fixture): seeding
    through that bucket is invisible to this file's `client`, which reads
    through `settings.s3_bucket`'s default. This fixture targets the app's
    bucket directly instead, so `store.put_json` here and the app's
    `document_artifact` route see the same object.
    """
    from fdi.platform.storage.artifact_store import ArtifactStore

    s = ArtifactStore(minio_client, bucket="artifacts")
    await s.ensure_bucket()
    return s


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
    # No run recorded yet -> every registered root stage is stale, ping among them.
    # Asserted by membership, not an exact list, so registering further root stages
    # (e.g. "ocr" from SP-5) does not make this test brittle.
    assert "ping" in (await client.get(f"/docs/{doc_id}/stale")).json()["stale"]

    # Seed a completed ping run; ping is no longer stale (other stages may remain).
    rid = str(uuid.uuid4())
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.start_run(rid, doc_id, "ping", 1, root_input_hash(None), None)
        await repo.finish_run(
            rid, output_hash="oh", artifact_key=f"artifacts/{doc_id}/ping/v1/x.json"
        )
        await s.commit()
    assert "ping" not in (await client.get(f"/docs/{doc_id}/stale")).json()["stale"]


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


# --------------------------------------------------------------------------
# Task 7: POST /documents/{id}/ocr
# --------------------------------------------------------------------------


async def test_trigger_ocr_on_a_document(client: AsyncClient, _alembic_applied) -> None:
    pdf = f"PDF-OCR-{uuid.uuid4()}".encode()
    up = await client.post(
        "/documents", files={"file": ("a.pdf", pdf, "application/pdf")}
    )
    doc_id = up.json()["id"]
    r = await client.post(f"/documents/{doc_id}/ocr")
    assert r.status_code == 202
    assert r.json() == {"doc_id": doc_id, "stage": "ocr"}


async def test_trigger_ocr_unknown_document_is_404(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/documents/00000000-0000-0000-0000-000000000000/ocr")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Task 7 (SP-10): POST /documents/{id}/semantic
# --------------------------------------------------------------------------


async def test_trigger_semantic_on_a_document(client: AsyncClient, _alembic_applied) -> None:
    pdf = f"PDF-SEMANTIC-{uuid.uuid4()}".encode()
    up = await client.post(
        "/documents", files={"file": ("a.pdf", pdf, "application/pdf")}
    )
    doc_id = up.json()["id"]
    r = await client.post(f"/documents/{doc_id}/semantic")
    assert r.status_code == 202
    assert r.json() == {"doc_id": doc_id, "stage": "semantic"}


async def test_trigger_semantic_unknown_document_is_404(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/documents/00000000-0000-0000-0000-000000000000/semantic")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Task 6 (SP-13): POST /documents/{id}/embedding
# --------------------------------------------------------------------------


async def test_trigger_embedding_on_a_document(client: AsyncClient, _alembic_applied) -> None:
    pdf = f"PDF-EMBEDDING-{uuid.uuid4()}".encode()
    up = await client.post(
        "/documents", files={"file": ("a.pdf", pdf, "application/pdf")}
    )
    doc_id = up.json()["id"]
    r = await client.post(f"/documents/{doc_id}/embedding")
    assert r.status_code == 202
    assert r.json() == {"doc_id": doc_id, "stage": "embedding"}


async def test_trigger_embedding_unknown_document_is_404(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/documents/00000000-0000-0000-0000-000000000000/embedding")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Task 5 (SP-9b): POST /documents/{id}/graph_load
# --------------------------------------------------------------------------


async def test_trigger_graph_load_on_a_document(client: AsyncClient, _alembic_applied) -> None:
    pdf = f"PDF-GRAPHLOAD-{uuid.uuid4()}".encode()
    up = await client.post("/documents", files={"file": ("a.pdf", pdf, "application/pdf")})
    doc_id = up.json()["id"]
    r = await client.post(f"/documents/{doc_id}/graph_load")
    assert r.status_code == 202
    assert r.json() == {"doc_id": doc_id, "stage": "graph_load"}


async def test_trigger_graph_load_unknown_document_is_404(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/documents/00000000-0000-0000-0000-000000000000/graph_load")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Task 1 (SP-18): GET /documents/{id}/stages + GET /documents/{id}/artifacts/{stage}
# --------------------------------------------------------------------------


async def test_stages_and_artifact_proxy(
    client: AsyncClient, _alembic_applied, session_factory, store
) -> None:
    from fdi.platform.db.repository import DocumentRepository, RegistryRepository

    doc = str(uuid.uuid4())
    checksum = str(uuid.uuid4())
    async with session_factory() as s:
        await DocumentRepository(s).create(
            id=doc,
            checksum=checksum,
            filename="a.pdf",
            byte_size=3,
            content_type="application/pdf",
            doc_key=doc,
            version=1,
        )
        await s.commit()

    # an OCR run that finished with an artifact, and a queued normalize run
    await store.put_json(f"artifacts/{doc}/ocr/v1/x.json", {"content": "ACME", "schema_version": 1})
    async with session_factory() as s:
        repo = RegistryRepository(s)
        r1 = str(uuid.uuid4())
        await repo.start_run(r1, doc, "ocr", 1, "ih", None)
        await repo.finish_run(r1, output_hash="oh", artifact_key=f"artifacts/{doc}/ocr/v1/x.json")
        await repo.start_run(str(uuid.uuid4()), doc, "normalize", 1, "ih2", None)  # left running
        await s.commit()

    stages = (await client.get(f"/documents/{doc}/stages")).json()["stages"]
    by = {st["name"]: st["status"] for st in stages}
    assert by["ocr"] == "done" and by["normalize"] == "running"

    art = await client.get(f"/documents/{doc}/artifacts/ocr")
    assert art.status_code == 200 and art.json() == {"content": "ACME", "schema_version": 1}

    assert (await client.get(f"/documents/{doc}/artifacts/normalize")).status_code == 404
    assert (await client.get("/documents/nope/stages")).status_code == 404


# --------------------------------------------------------------------------
# Task 6 (SP-14): POST /search
# --------------------------------------------------------------------------


async def test_search_503_when_vector_store_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/search", json={"query": "Balance Sheet"})
    assert r.status_code == 503
    assert "vector_store" in r.json()["detail"]
    assert "embedder" not in r.json()["detail"]


async def test_search_returns_the_best_match_first(make_client, _alembic_applied) -> None:
    from fdi.api.deps import get_embedder, get_vector_store
    from fdi.pipeline.embeddings import FakeEmbeddingClient
    from fdi.pipeline.vectorstore import FakeVectorStore

    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_store = FakeVectorStore()
    fake_store.upsert(
        [
            {
                "id": "doc1:s0",
                "vector": fake_embedder.embed(["Balance Sheet\nAssets 100"])[0],
                "metadata": {"heading": "Balance Sheet", "chunk_id": "s0"},
            },
            {
                "id": "doc1:s1",
                "vector": fake_embedder.embed(["Notes\nSee appendix"])[0],
                "metadata": {"heading": "Notes", "chunk_id": "s1"},
            },
        ]
    )

    async with make_client(
        dependency_overrides={
            get_embedder: lambda: fake_embedder,
            get_vector_store: lambda: fake_store,
        }
    ) as client:
        r = await client.post("/search", json={"query": "Balance Sheet\nAssets 100", "k": 1})

    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["id"] == "doc1:s0"
    assert body["results"][0]["metadata"]["heading"] == "Balance Sheet"
    # A real (non-trivial) embedding vector's self-similarity is only
    # approximately 1.0 in floating point: `_cosine` divides by
    # `sqrt(sum(x*x))`, and `sqrt(x)**2 != x` bit-for-bit in IEEE754 except
    # for perfect squares -- measured here as `1.0000000000000002`, not `1.0`
    # exactly. `test_vectorstore.py`'s own `== 1.0` assertions only hold
    # because THAT test's vectors ([1.0, 0.0]) have an exact unit norm.
    assert body["results"][0]["score"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# RT-2: POST /retrieve/vector -- text + citations, section_type/doc_key/version filters.
# --------------------------------------------------------------------------


async def test_retrieve_vector_503_when_unconfigured(client: AsyncClient, _alembic_applied) -> None:
    r = await client.post("/retrieve/vector", json={"query": "Balance Sheet"})
    assert r.status_code == 503
    assert "vector_store" in r.json()["detail"]


async def _seed_processed_doc(session_factory, store) -> dict[str, object]:
    """One fully-processed document (real chunks artifact + a real Document
    row), for exercising the route's Postgres join / resolve_chunks path --
    NOT the raw synthetic-vector shape /search's own test uses."""
    from fdi.core.semantic import SectionType
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.semantic import FakeSemanticClient
    from fdi.pipeline.stages.ast import AstStage
    from fdi.pipeline.stages.chunks import ChunksStage
    from fdi.pipeline.stages.graph import GraphStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.provenance import ProvenanceStage
    from fdi.pipeline.stages.semantic import SemanticStage
    from fdi.pipeline.stages.structure import StructureStage

    doc_key = f"annual-{uuid.uuid4()}"
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    fake_ocr = FakeOcrClient({
        "content": "ACME",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "sectionHeading", "content": "Balance Sheet"},
            {"content": "Total Assets 100"},
        ],
        "tables": [],
    })
    fake_semantic = FakeSemanticClient({"Balance Sheet": SectionType.BALANCE_SHEET.value})

    reg = StageRegistry()
    for stage in (
        OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
        ProvenanceStage(), StructureStage(), ChunksStage(),
    ):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()),
        services={"semantic": fake_semantic}, **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    return {"doc_id": doc.id, "doc_key": doc_key}


async def test_retrieve_vector_returns_text_and_citation(
    make_client, _alembic_applied, session_factory, store
) -> None:
    from fdi.api.deps import get_embedder, get_vector_store
    from fdi.pipeline.embeddings import FakeEmbeddingClient
    from fdi.pipeline.vectorstore import FakeVectorStore

    seeded = await _seed_processed_doc(session_factory, store)
    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_store = FakeVectorStore()
    fake_store.upsert([{
        "id": f"{seeded['doc_id']}:s0",
        "vector": fake_embedder.embed(["Balance Sheet\nTotal Assets 100"])[0],
        "metadata": {"heading": "Balance Sheet", "chunk_id": "s0",
                      "section_type": "BALANCE_SHEET", "page": 1,
                      "text": "Balance Sheet\nTotal Assets 100"},
    }])

    async with make_client(
        dependency_overrides={
            get_embedder: lambda: fake_embedder,
            get_vector_store: lambda: fake_store,
        }
    ) as client:
        r = await client.post(
            "/retrieve/vector",
            json={"query": "Balance Sheet\nTotal Assets 100", "k": 1},
        )

    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["doc_id"] == seeded["doc_id"]
    assert result["doc_key"] == seeded["doc_key"]
    assert result["version"] == 1
    assert result["chunk_id"] == "s0"
    assert result["section_type"] == "BALANCE_SHEET"
    assert "Total Assets 100" in result["text"]
    assert result["score"] == pytest.approx(1.0)


async def test_retrieve_vector_section_type_filter(
    make_client, _alembic_applied, session_factory, store
) -> None:
    from fdi.api.deps import get_embedder, get_vector_store
    from fdi.pipeline.embeddings import FakeEmbeddingClient
    from fdi.pipeline.vectorstore import FakeVectorStore

    seeded = await _seed_processed_doc(session_factory, store)
    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_store = FakeVectorStore()
    fake_store.upsert([{
        "id": f"{seeded['doc_id']}:s0",
        "vector": fake_embedder.embed(["Balance Sheet\nTotal Assets 100"])[0],
        "metadata": {"heading": "Balance Sheet", "chunk_id": "s0",
                      "section_type": "BALANCE_SHEET", "page": 1},
    }])

    async with make_client(
        dependency_overrides={
            get_embedder: lambda: fake_embedder,
            get_vector_store: lambda: fake_store,
        }
    ) as client:
        miss = await client.post(
            "/retrieve/vector",
            json={
                "query": "Balance Sheet\nTotal Assets 100", "k": 5,
                "filters": {"section_type": "NOTES"},
            },
        )
        hit = await client.post(
            "/retrieve/vector",
            json={
                "query": "Balance Sheet\nTotal Assets 100", "k": 5,
                "filters": {"section_type": "BALANCE_SHEET"},
            },
        )

    assert miss.json()["results"] == []
    assert len(hit.json()["results"]) == 1


# --------------------------------------------------------------------------
# RT-9: POST /retrieve -- hybrid (RRF) retrieval over RT-2's vector_search +
# RT-9's bm25_search (Milvus-native BM25). POST /documents/{id}/index_fts and
# the Postgres-backed /retrieve/keyword are GONE; /retrieve/keyword is kept
# and repointed at bm25_search instead. Supersedes RT-4 decision 2: with both
# channels sharing one Milvus VectorStore seam there is nothing left to
# degrade to, so an unconfigured vector_store now 503s on /retrieve and
# /retrieve/keyword, matching /retrieve/vector -- NOT the keyword-only
# degrade this section used to test.
# --------------------------------------------------------------------------


async def test_retrieve_hybrid_503s_when_vector_store_unconfigured(make_client) -> None:
    """RT-4 decision 2 is superseded: with both channels in Milvus there is
    nothing to degrade to, so an unconfigured Milvus is a configuration error
    and says so -- rather than returning a confident empty list."""
    async with make_client() as client:
        r = await client.post("/retrieve", json={"query": "revenue", "k": 5})
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]


async def test_retrieve_keyword_503s_when_vector_store_unconfigured(make_client) -> None:
    async with make_client() as client:
        r = await client.post("/retrieve/keyword", json={"query": "revenue", "k": 5})
    assert r.status_code == 503


async def test_index_fts_route_is_gone(make_client) -> None:
    async with make_client() as client:
        r = await client.post("/documents/whatever/index_fts")
    assert r.status_code == 404


# --------------------------------------------------------------------------
# Task 6 (SP-16): POST /entities/resolve
# --------------------------------------------------------------------------
#
# `resolved_corpus_via_api` drives three documents through the full stage
# chain DIRECTLY (via run_stage against this file's own `session_factory` /
# `store` fixtures -- bucket "artifacts", the same bucket `client`'s app
# reads through settings.s3_bucket's default), not through the HTTP trigger
# endpoints: those only ever `dispatch()` to Celery, which nothing in this
# suite runs, so a stage_run would never actually reach "done". This mirrors
# test_resolve_entities.py's `resolved_corpus` fixture one layer up (API
# instead of the bare pipeline function).
#
# Counts are asserted by which of THIS fixture's own three doc ids share an
# entity, not by the response's raw `entity_count` -- `/entities/resolve`
# scans the WHOLE corpus (spec decision 8, no per-call scoping) and this
# fixture may run more than once across sibling tests in the same
# session-scoped Postgres, so a raw total is not a stable count. See
# test_resolve_entities.py's module docstring for the full rationale.


class _SequencedEmbeddingClient:
    """Deterministic, call-ordered stand-in -- see test_resolve_entities.py."""

    model = "fake-sequenced"

    def __init__(self, vectors: list[list[float]]) -> None:
        self._vectors = vectors
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        vector = self._vectors[self.calls]
        self.calls += 1
        return [vector for _ in texts]


@pytest.fixture
async def resolved_corpus_via_api(session_factory, store) -> dict[str, str]:
    """Three fully-processed documents: A, B near-identical (cosine ~0.99),
    C orthogonal to both -- same numbers as test_resolve_entities.py."""
    from fdi.core.semantic import SectionType
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.semantic import FakeSemanticClient
    from fdi.pipeline.stages.ast import AstStage
    from fdi.pipeline.stages.chunks import ChunksStage
    from fdi.pipeline.stages.embedding import EmbeddingStage
    from fdi.pipeline.stages.graph import GraphStage
    from fdi.pipeline.stages.knowledge import KnowledgeStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.ontology import OntologyStage
    from fdi.pipeline.stages.provenance import ProvenanceStage
    from fdi.pipeline.stages.quality import QualityStage
    from fdi.pipeline.stages.semantic import SemanticStage
    from fdi.pipeline.stages.structure import StructureStage

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(SemanticStage())
    reg.register(GraphStage())
    reg.register(ProvenanceStage())
    reg.register(ChunksStage())
    reg.register(OntologyStage())
    reg.register(QualityStage())
    reg.register(KnowledgeStage())
    reg.register(EmbeddingStage())
    resolver = DagResolver(reg.all())

    fake_ocr = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD Annual Report"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Total Assets"},
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient({"Balance Sheet": SectionType.BALANCE_SHEET.value})
    embedder = _SequencedEmbeddingClient([[1.0, 0.0], [0.99, 0.14], [0.0, 1.0]])

    ids: dict[str, str] = {}
    for key in ("a", "b", "c"):
        doc = await intake_document(
            pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
            filename=f"{key}.pdf",
            doc_key=str(uuid.uuid4()),
            content_type="application/pdf",
            session_factory=session_factory,
            store=store,
        )
        common = {
            "doc_id": doc.id,
            "correlation_id": "c1",
            "session_factory": session_factory,
            "store": store,
            "registry": reg,
            "resolver": resolver,
        }
        await run_stage(
            stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
        )
        await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(
            stage_name="semantic",
            run_id=str(uuid.uuid4()),
            services={"semantic": fake_semantic},
            **common,
        )
        await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="ontology", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="quality", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(stage_name="knowledge", run_id=str(uuid.uuid4()), services={}, **common)
        await run_stage(
            stage_name="embedding",
            run_id=str(uuid.uuid4()),
            services={"embedder": embedder},
            **common,
        )
        ids[key] = doc.id
    return ids


def _entity_ids_for(body: dict, doc_ids: dict[str, str]) -> dict[str, str]:
    """{"a": <entity_id>, "b": ..., "c": ...} -- which entity each of our own
    three documents landed in, read off the response body."""
    result: dict[str, str] = {}
    for key, doc_id in doc_ids.items():
        entity = next(
            e for e in body["entities"] if doc_id in [m["doc_id"] for m in e["members"]]
        )
        result[key] = entity["entity_id"]
    return result


async def test_entities_resolve_returns_entities(
    client: AsyncClient, _alembic_applied, resolved_corpus_via_api
) -> None:
    resp = await client.post("/entities/resolve", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["threshold"] == 0.85  # configured default

    by = _entity_ids_for(body, resolved_corpus_via_api)
    assert by["a"] == by["b"]  # A, B cluster together at the default threshold
    assert by["a"] != by["c"]  # C is a separate entity


async def test_entities_resolve_threshold_override(
    client: AsyncClient, _alembic_applied, resolved_corpus_via_api
) -> None:
    resp = await client.post("/entities/resolve", json={"threshold": 0.999})
    assert resp.status_code == 200
    body = resp.json()
    assert body["threshold"] == 0.999

    by = _entity_ids_for(body, resolved_corpus_via_api)
    assert len({by["a"], by["b"], by["c"]}) == 3  # A, B no longer merge


async def test_entities_resolve_rejects_out_of_range_threshold(
    client: AsyncClient, _alembic_applied
) -> None:
    resp = await client.post("/entities/resolve", json={"threshold": 1.5})
    assert resp.status_code == 422


async def test_entities_resolve_valid_shape_with_no_corpus_setup(
    client: AsyncClient, _alembic_applied
) -> None:
    """No 503, no raise -- entity resolution needs neither Milvus nor an
    embedder at request time (spec §3.3), unlike /search. A literal empty
    corpus is not assertable here (see test_resolve_entities.py's module
    docstring: Postgres is session-scoped across the whole integration
    suite), so this pins the documented shape and the echoed default
    threshold instead."""
    resp = await client.post("/entities/resolve", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == 1
    assert body["threshold"] == 0.85
    assert isinstance(body["entity_count"], int)
    assert isinstance(body["entities"], list)
    assert isinstance(body["unresolved"], list)
    assert body["entity_count"] == len(body["entities"])


# --------------------------------------------------------------------------
# RT-6: GET /documents/{doc_id}/tables, GET /documents/{doc_id}/concepts/{concept}
# --------------------------------------------------------------------------


@pytest.fixture
async def doc_with_numeric_table(session_factory, store) -> str:
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.structure import StructureStage

    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    fake_ocr = FakeOcrClient(
        {
            "content": "ACME LTD",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {
                    "role": "title", "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 0, "length": 8}],
                },
            ],
            "tables": [
                {
                    "rowCount": 2, "columnCount": 2,
                    "spans": [{"offset": 20, "length": 40}],
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "cells": [
                        {
                            "rowIndex": 0, "columnIndex": 0,
                            "kind": "columnHeader", "content": "Particulars",
                        },
                        {
                            "rowIndex": 0, "columnIndex": 1,
                            "kind": "columnHeader", "content": "FY24",
                        },
                        {
                            "rowIndex": 1, "columnIndex": 0,
                            "content": "Total revenue from operations",
                        },
                        {"rowIndex": 1, "columnIndex": 1, "content": "12,345"},
                    ],
                },
            ],
            "sections": [],
        }
    )
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(StructureStage())
    resolver = DagResolver(reg.all())
    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    return doc.id


async def test_get_tables_route(
    client: AsyncClient, _alembic_applied, doc_with_numeric_table
) -> None:
    resp = await client.get(f"/documents/{doc_with_numeric_table}/tables")
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_id"] == doc_with_numeric_table
    assert len(body["tables"]) == 1
    assert body["tables"][0]["table_index"] == 0
    assert body["tables"][0]["grid"]["column_headers"] == ["Particulars", "FY24"]


async def test_get_tables_route_keyword_filter(
    client: AsyncClient, _alembic_applied, doc_with_numeric_table
) -> None:
    resp = await client.get(
        f"/documents/{doc_with_numeric_table}/tables", params={"keyword": "fy99"}
    )
    assert resp.status_code == 200
    assert resp.json()["tables"] == []


async def test_get_tables_route_404_unknown_doc(client: AsyncClient, _alembic_applied) -> None:
    resp = await client.get("/documents/no-such-doc/tables")
    assert resp.status_code == 404


async def test_concept_lookup_route(
    client: AsyncClient, _alembic_applied, doc_with_numeric_table
) -> None:
    resp = await client.get(f"/documents/{doc_with_numeric_table}/concepts/REVENUE")
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_id"] == doc_with_numeric_table
    assert body["concept"] == "REVENUE"
    assert body["rows"] == [{
        "table_index": 0,
        "row_label": "Total revenue from operations",
        "values": [{"column_header": "FY24", "raw": "12,345", "value": 12345.0}],
    }]


async def test_concept_lookup_route_invalid_concept_422(
    client: AsyncClient, _alembic_applied, doc_with_numeric_table
) -> None:
    resp = await client.get(f"/documents/{doc_with_numeric_table}/concepts/NOT_A_CONCEPT")
    assert resp.status_code == 422


async def test_concept_lookup_route_404_unknown_doc(
    client: AsyncClient, _alembic_applied
) -> None:
    resp = await client.get("/documents/no-such-doc/concepts/REVENUE")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# RT-7: GET /entities/{doc_key}/timeline, POST /entities/compare
# --------------------------------------------------------------------------


@pytest.fixture
async def two_version_doc_key_via_api(session_factory, store) -> str:
    """Two documents, SAME doc_key, DIFFERENT pdf bytes (distinct checksums ->
    version 1 revenue FY24=12,345, version 2 revenue FY25=15,000). Mirrors
    doc_with_numeric_table's OCR shape (RT-6, immediately above) but run
    TWICE under one doc_key to exercise RT-7's cross-version comparison."""
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.structure import StructureStage

    def _ocr(header: str, value: str) -> FakeOcrClient:
        return FakeOcrClient({
            "content": "ACME LTD",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [{
                "role": "title", "content": "ACME LTD",
                "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                "spans": [{"offset": 0, "length": 8}],
            }],
            "tables": [{
                "rowCount": 2, "columnCount": 2,
                "spans": [{"offset": 20, "length": 40}],
                "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                     "content": "Particulars"},
                    {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": header},
                    {"rowIndex": 1, "columnIndex": 0, "content": "Total revenue from operations"},
                    {"rowIndex": 1, "columnIndex": 1, "content": value},
                ],
            }],
            "sections": [],
        })

    doc_key = str(uuid.uuid4())
    for header, value in (("FY24", "12,345"), ("FY25", "15,000")):
        doc = await intake_document(
            pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
            filename="a.pdf", doc_key=doc_key, content_type="application/pdf",
            session_factory=session_factory, store=store,
        )
        reg = StageRegistry()
        reg.register(OcrStage())
        reg.register(StructureStage())
        resolver = DagResolver(reg.all())
        common = {
            "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
            "store": store, "registry": reg, "resolver": resolver,
        }
        await run_stage(
            stage_name="ocr", run_id=str(uuid.uuid4()),
            services={"ocr": _ocr(header, value)}, **common,
        )
        await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    return doc_key


async def test_compare_route_matched_across_versions(
    client: AsyncClient, _alembic_applied, two_version_doc_key_via_api
) -> None:
    resp = await client.post(
        "/entities/compare",
        json={"doc_key": two_version_doc_key_via_api, "concept": "REVENUE"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_key"] == two_version_doc_key_via_api
    assert body["concept"] == "REVENUE"
    assert [v["status"] for v in body["versions"]] == ["matched", "matched"]
    assert body["versions"][0]["rows"][0]["values"][0] == {
        "column_header": "FY24", "raw": "12,345", "value": 12345.0,
    }
    assert body["versions"][1]["rows"][0]["values"][0] == {
        "column_header": "FY25", "raw": "15,000", "value": 15000.0,
    }


async def test_compare_route_404_unknown_doc_key(
    client: AsyncClient, _alembic_applied
) -> None:
    resp = await client.post(
        "/entities/compare", json={"doc_key": "no-such-key", "concept": "REVENUE"}
    )
    assert resp.status_code == 404


async def test_entity_timeline_route(
    client: AsyncClient, _alembic_applied, two_version_doc_key_via_api
) -> None:
    resp = await client.get(f"/entities/{two_version_doc_key_via_api}/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["doc_key"] == two_version_doc_key_via_api
    assert body["member_count"] == 2
    assert "entity_id" not in body
    assert [t["version"] for t in body["timeline"]] == [1, 2]


async def test_entity_timeline_route_404_unknown_doc_key(
    client: AsyncClient, _alembic_applied
) -> None:
    resp = await client.get("/entities/no-such-key/timeline")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# RT-5: graph retrieval routes -- 503 when graph_store is unconfigured.
# --------------------------------------------------------------------------


async def test_graph_neighbors_503_when_graph_store_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/graph/neighbors", json={"node_id": "d1:s0"})
    assert r.status_code == 503
    assert "graph_store" in r.json()["detail"]


async def test_document_structure_503_when_graph_store_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.get("/documents/no-such-doc/graph/structure")
    assert r.status_code == 503
    assert "graph_store" in r.json()["detail"]


async def test_reading_order_503_when_graph_store_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.get("/documents/no-such-doc/graph/reading-order")
    assert r.status_code == 503
    assert "graph_store" in r.json()["detail"]


# --------------------------------------------------------------------------
# RT-5: graph retrieval routes -- happy path (FakeGraphStore override).
# --------------------------------------------------------------------------


async def test_graph_neighbors_returns_cited_hits(make_client, _alembic_applied) -> None:
    from fdi.api.deps import get_graph_store
    from fdi.pipeline.graphstore import FakeGraphStore

    fake_store = FakeGraphStore()
    fake_store.load(
        [
            {"id": "d1:doc", "type": "document", "label": "ACME", "doc_id": "d1"},
            {"id": "d1:s0", "type": "section", "label": "Balance Sheet", "doc_id": "d1"},
        ],
        [{"source": "d1:doc", "target": "d1:s0", "type": "CONTAINS"}],
    )

    async with make_client(
        dependency_overrides={get_graph_store: lambda: fake_store}
    ) as client:
        r = await client.post("/graph/neighbors", json={"node_id": "d1:doc"})
    assert r.status_code == 200
    body = r.json()
    assert body["node_id"] == "d1:doc"
    hit = body["neighbors"][0]
    assert hit["node"]["id"] == "d1:s0"
    assert hit["relationship"] == "CONTAINS"
    assert hit["node"]["citation"]["heading"] == "Balance Sheet"


async def test_document_structure_route_returns_tree(
    make_client, session_factory, store, _alembic_applied
) -> None:
    """`session_factory`/`store` are THIS FILE's own fixtures (defined above,
    around the `client`/`make_client` fixtures) -- `store` is deliberately
    pinned to bucket "artifacts", the same bucket `make_client`'s app opens
    by default, so seeding through it here is visible to the app's routes.
    Do not reconstruct an engine/ArtifactStore by hand; these already exist.
    """
    from fdi.api.deps import get_graph_store
    from fdi.pipeline.graphstore import FakeGraphStore
    from fdi.pipeline.intake import intake_document

    fake_store = FakeGraphStore()
    doc = await intake_document(
        pdf=b"%PDF-1.7\n%fake\n", filename="a.pdf", doc_key="k",
        content_type="application/pdf", session_factory=session_factory, store=store,
    )
    fake_store.load(
        [
            {"id": f"{doc.id}:doc", "type": "document", "label": "ACME", "doc_id": doc.id},
            {"id": f"{doc.id}:s0", "type": "section", "label": "S0", "doc_id": doc.id},
        ],
        [{"source": f"{doc.id}:doc", "target": f"{doc.id}:s0", "type": "CONTAINS"}],
    )

    async with make_client(
        dependency_overrides={get_graph_store: lambda: fake_store}
    ) as client:
        r = await client.get(f"/documents/{doc.id}/graph/structure")
        assert r.status_code == 200
        body = r.json()
        assert body["doc_id"] == doc.id
        assert body["document"]["id"] == f"{doc.id}:doc"
        assert body["sections"][0]["id"] == f"{doc.id}:s0"

        r404 = await client.get("/documents/no-such-doc/graph/structure")
        assert r404.status_code == 404


async def test_reading_order_route_returns_sections_without_paragraphs(
    make_client, session_factory, store, _alembic_applied
) -> None:
    from fdi.api.deps import get_graph_store
    from fdi.pipeline.graphstore import FakeGraphStore
    from fdi.pipeline.intake import intake_document

    fake_store = FakeGraphStore()
    doc = await intake_document(
        pdf=b"%PDF-1.7\n%fake\n", filename="a.pdf", doc_key="k",
        content_type="application/pdf", session_factory=session_factory, store=store,
    )
    fake_store.load(
        [{"id": f"{doc.id}:doc", "type": "document", "label": "ACME", "doc_id": doc.id},
         {"id": f"{doc.id}:s0", "type": "section", "label": "S0", "doc_id": doc.id}],
        [{"source": f"{doc.id}:doc", "target": f"{doc.id}:s0", "type": "CONTAINS"}],
    )

    async with make_client(
        dependency_overrides={get_graph_store: lambda: fake_store}
    ) as client:
        r = await client.get(f"/documents/{doc.id}/graph/reading-order")
        assert r.status_code == 200
        body = r.json()
        assert body["sections"] == [
            {
                "id": f"{doc.id}:s0", "type": "section", "label": "S0", "doc_id": doc.id,
                "citation": {
                    "doc_id": doc.id, "doc_key": "k", "version": 1, "heading": "S0",
                    "page": None, "bounding_regions": None,
                },
            }
        ]


# --------------------------------------------------------------------------
# RT-8: POST /ask -- the retrieval agent.
# --------------------------------------------------------------------------


async def test_ask_503_when_agent_client_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/ask", json={"question": "What was revenue?"})
    assert r.status_code == 503
    assert "agent_client" in r.json()["detail"]


async def test_ask_returns_cited_answer_with_fake_agent_client_override(
    make_client, session_factory, store, _alembic_applied
) -> None:
    import uuid

    from fdi.api.deps import get_agent_client, get_embedder, get_vector_store
    from fdi.core.agent import AgentResponse, ToolCall
    from fdi.pipeline.agent import FakeAgentClient
    from fdi.pipeline.embeddings import FakeEmbeddingClient
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.semantic import FakeSemanticClient
    from fdi.pipeline.stages.ast import AstStage
    from fdi.pipeline.stages.chunks import ChunksStage
    from fdi.pipeline.stages.graph import GraphStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.provenance import ProvenanceStage
    from fdi.pipeline.stages.semantic import SemanticStage
    from fdi.pipeline.stages.structure import StructureStage
    from fdi.pipeline.vectorstore import FakeVectorStore

    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), StructureStage(), ChunksStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    fake_ocr = FakeOcrClient({
        "content": "ACME",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "sectionHeading", "content": "Balance Sheet"},
            {"content": f"Total Assets 100 {marker}"},
        ],
        "tables": [],
    })
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="bs.pdf",
        doc_key=f"acme-{marker}", content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    common = {"doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
              "store": store, "registry": reg, "resolver": resolver}
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()),
        services={"semantic": FakeSemanticClient({"Balance Sheet": "BALANCE_SHEET"})}, **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)

    # RT-9: the `search` tool's hybrid_search now requires a configured
    # embedder/vector_store (no more Postgres-FTS-only degrade), so this
    # override supplies a Fake pair with the chunk pre-indexed -- exactly
    # what IndexStage would have written into Milvus.
    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_vector_store = FakeVectorStore()
    fake_vector_store.upsert([{
        "id": f"{doc.id}:s0",
        "vector": fake_embedder.embed([f"Total Assets 100 {marker}"])[0],
        "metadata": {"heading": "Balance Sheet", "chunk_id": "s0",
                      "section_type": "BALANCE_SHEET", "page": 1,
                      "text": f"Total Assets 100 {marker}"},
    }])

    fake = FakeAgentClient([
        AgentResponse(tool_calls=[ToolCall(
            id="c1", name="search", arguments={"query": f"Total Assets 100 {marker}"}
        )]),
        AgentResponse(content=f"Total Assets were 100 (see {doc.id}:s0)."),
    ])

    async with make_client(
        dependency_overrides={
            get_agent_client: lambda: fake,
            get_embedder: lambda: fake_embedder,
            get_vector_store: lambda: fake_vector_store,
        }
    ) as client:
        r = await client.post("/ask", json={"question": f"total assets, marker {marker}?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == f"Total Assets were 100 (see {doc.id}:s0)."
    assert body["citations"]
    assert body["tool_trace"][0]["tool"] == "search"


# --------------------------------------------------------------------------
# SP-17: POST /extract/document -- schema-driven RAG field extraction.
# --------------------------------------------------------------------------


async def test_extract_document_503_when_extractor_client_unconfigured(
    client: AsyncClient, _alembic_applied
) -> None:
    r = await client.post("/extract/document", json={"doc_key": "no-such-key"})
    assert r.status_code == 503
    assert "extractor_client" in r.json()["detail"]


async def test_extract_document_404_unknown_doc_key(make_client, _alembic_applied) -> None:
    from fdi.api.deps import get_extractor_client
    from fdi.pipeline.extractor import FakeExtractorClient

    async with make_client(
        dependency_overrides={get_extractor_client: lambda: FakeExtractorClient([])}
    ) as client:
        r = await client.post("/extract/document", json={"doc_key": "no-such-key"})
    assert r.status_code == 404


async def test_extract_document_happy_path_with_fake_extractor_client_override(
    make_client, session_factory, store, tmp_path, _alembic_applied
) -> None:
    import json
    import uuid

    from fdi.api.deps import (
        get_embedder,
        get_extraction_schema_path,
        get_extractor_client,
        get_vector_store,
    )
    from fdi.pipeline.embeddings import FakeEmbeddingClient
    from fdi.pipeline.extractor import FakeExtractorClient
    from fdi.pipeline.intake import intake_document
    from fdi.pipeline.ocr import FakeOcrClient
    from fdi.pipeline.registry import StageRegistry
    from fdi.pipeline.resolver import DagResolver
    from fdi.pipeline.run import run_stage
    from fdi.pipeline.semantic import FakeSemanticClient
    from fdi.pipeline.stages.ast import AstStage
    from fdi.pipeline.stages.chunks import ChunksStage
    from fdi.pipeline.stages.graph import GraphStage
    from fdi.pipeline.stages.normalize import NormalizeStage
    from fdi.pipeline.stages.ocr import OcrStage
    from fdi.pipeline.stages.provenance import ProvenanceStage
    from fdi.pipeline.stages.semantic import SemanticStage
    from fdi.pipeline.stages.structure import StructureStage
    from fdi.pipeline.vectorstore import FakeVectorStore

    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), StructureStage(), ChunksStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    fake_ocr = FakeOcrClient({
        "content": "ACME",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "sectionHeading", "content": "Overview"},
            {"content": f"ACME is a manufacturer {marker}."},
        ],
        "tables": [],
    })
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
        doc_key=f"acme-{marker}", content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    common = {"doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
              "store": store, "registry": reg, "resolver": resolver}
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()),
        services={"semantic": FakeSemanticClient({"Overview": "MDNA"})}, **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)

    schema = {
        "version": "3.1.0",
        "modules": [{
            "id": 1, "key": "module_1_overview", "name": "Overview",
            "retrieval_units": [{
                "unit_id": "RU_OVERVIEW", "title": "Company Overview",
                "scope": "front_matter", "locator_keywords": ["manufacturer"],
                "retrieval_query": f"manufacturer {marker}",
                "fields": [{
                    "field_name": "Company overview text",
                    "aliases": ["overview"],
                    "data_type": "text",
                    "extraction_prompt": "Extract the overview.",
                }],
            }],
        }],
    }
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")

    # RT-9: _pool_for's hybrid_search now requires a configured embedder/
    # vector_store (no more Postgres-FTS-only degrade) -- without it the pool
    # is empty, extract_unit short-circuits on `if not ranked`, and the
    # scripted FakeExtractorClient response is never even requested. This
    # override supplies a Fake pair with the chunk pre-indexed, exactly what
    # IndexStage would have written into Milvus.
    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_vector_store = FakeVectorStore()
    fake_vector_store.upsert([{
        "id": f"{doc.id}:s0",
        "vector": fake_embedder.embed([f"ACME is a manufacturer {marker}."])[0],
        "metadata": {"doc_id": doc.id, "heading": "Overview", "chunk_id": "s0", "page": 1,
                      "text": f"ACME is a manufacturer {marker}."},
    }])

    # One call per Retrieval Unit, not per field: the response is keyed by
    # field_path, falling back to field_name when the schema omits it (as
    # this one does).
    fake = FakeExtractorClient([
        {"Company overview text": {
            "value": "ACME is a manufacturer.",
            "source": {"page": 1, "snippet": "ACME is a manufacturer",
                       "evidence_ids": [f"{doc.id}:s0"]},
            "confidence": 0.9}},
    ])

    async with make_client(
        dependency_overrides={
            get_extractor_client: lambda: fake,
            get_extraction_schema_path: lambda: str(schema_path),
            get_embedder: lambda: fake_embedder,
            get_vector_store: lambda: fake_vector_store,
        }
    ) as client:
        r = await client.post("/extract/document", json={"doc_key": f"acme-{marker}"})
    assert r.status_code == 200
    body = r.json()
    assert body["doc_key"] == f"acme-{marker}"
    assert body["doc_id"] == doc.id
    assert body["module"] is None
    assert body["field_count"] == 1
    assert body["fields"][0]["field_name"] == "Company overview text"
    assert body["fields"][0]["value"] == "ACME is a manufacturer."
