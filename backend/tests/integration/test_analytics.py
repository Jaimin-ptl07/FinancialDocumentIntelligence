"""GET /analytics -- corpus totals and per-stage throughput.

Three things make this file look the way it does.

**The route is corpus-global and the container database is shared.** Every
other integration test in this session has already written documents and
stage_runs into the same Postgres, so an absolute assertion like
`document_count == 4` would pass or fail on collection order. Every
Postgres-backed figure here is therefore asserted as a DELTA against a
baseline read taken before seeding -- exact arithmetic, immune to whatever
else the suite left behind.

**The one figure that cannot be asserted as a delta is the empty corpus.**
`test_empty_corpus_...` deletes every row inside a transaction it never
commits, reads the aggregates through that same session, and rolls back. It
is the only way to observe the empty-corpus SQL (zeros, no NULL leaking into
an int field, no division by zero) without a second database.

**`chunk_count` is NOT a Postgres figure any more (RT-9).** The `chunk`
table is gone; the route now reads `VectorStore.count()`, which each
`create_app()` call starts fresh (no shared, cross-test accumulation the
way Postgres has) -- so `chunk_count` is asserted directly rather than as a
delta, by overriding `app.state.vector_store` with a `FakeVectorStore` the
test controls.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy
from httpx import ASGITransport, AsyncClient

from fdi.pipeline.vectorstore import FakeVectorStore
from fdi.platform.config import get_settings
from tests.conftest import TEST_SIGNING_SECRET
from tests.integration.conftest import authenticate

pytestmark = pytest.mark.integration


@pytest.fixture
async def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch, invitee):
    @asynccontextmanager
    async def _make(vector_store=None):
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
        async with app.router.lifespan_context(app):
            # No FDI_MILVUS_URI above -> lifespan already set
            # app.state.vector_store = None; overriding it here (AFTER
            # lifespan, so nothing in startup clobbers it) is what lets a
            # test control what GET /analytics's chunk_count reads without
            # a live Milvus instance.
            if vector_store is not None:
                app.state.vector_store = vector_store
            transport = ASGITransport(app=app, raise_app_exceptions=True)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await authenticate(client, invitee)
                yield client

    return _make


@pytest.fixture
def session_factory(migrated_engine):
    from fdi.platform.db.engine import make_session_factory

    return make_session_factory(migrated_engine)


def _at(second: int) -> datetime:
    """A fixed wall clock, so run durations are exact rather than ~0ms.

    `created_at`/`updated_at` are server_default=now(), which inside one
    transaction is the SAME instant for both -- a run seeded through
    start_run/finish_run has a duration of exactly 0.0s and could not tell a
    correct median from `0`. Passing the columns explicitly overrides the
    default and buys a median with a known answer.
    """
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=second)


async def _seed_docs(session_factory, prefix: str) -> list[str]:
    """4 documents over 3 doc_keys; byte_size 10 + 20 + 30 + NULL."""
    from fdi.platform.db.repository import DocumentRepository

    plan = [
        (f"{prefix}-a", 1, 10),
        (f"{prefix}-a", 2, 20),
        (f"{prefix}-b", 1, 30),
        (f"{prefix}-c", 1, None),
    ]
    ids: list[str] = []
    async with session_factory() as s:
        repo = DocumentRepository(s)
        for doc_key, version, size in plan:
            doc_id = str(uuid.uuid4())
            doc = await repo.create(
                id=doc_id,
                checksum=uuid.uuid4().hex,
                filename=f"{doc_key}-v{version}.pdf",
                byte_size=size or 0,
                content_type="application/pdf",
                doc_key=doc_key,
                version=version,
            )
            if size is None:  # exercise the NULL-as-0 branch of total_bytes
                doc.byte_size = None
            ids.append(doc_id)
        await s.commit()
    return ids


async def _seed_runs(session_factory, stage: str, doc_ids: list[str]) -> None:
    """One probe stage with a known shape, plus a terminal `index` run.

    probe stage, latest run per doc only:
      doc0 -> done, 10s      doc1 -> done, 30s
      doc2 -> done 5s SUPERSEDED by a later failed 60s run
    So done=2, failed=1, running=0, queued=0, median=20.0. If the query
    counted every run the median would be 15.0; if it took the median over
    all latest runs regardless of status it would be 30.0.
    """
    from fdi.platform.db.models import StageRun

    doc0, doc1, doc2, _doc3 = doc_ids
    rows = [
        (doc0, stage, "done", _at(0), _at(10)),
        (doc1, stage, "done", _at(0), _at(30)),
        (doc2, stage, "done", _at(0), _at(5)),
        (doc2, stage, "failed", _at(10), _at(70)),
        (doc0, "index", "done", _at(0), _at(1)),
    ]
    async with session_factory() as s:
        for doc_id, stage_name, status, created, updated in rows:
            s.add(
                StageRun(
                    id=str(uuid.uuid4()),
                    doc_id=doc_id,
                    stage_name=stage_name,
                    stage_version=1,
                    input_hash="h",
                    status=status,
                    created_at=created,
                    updated_at=updated,
                )
            )
        await s.commit()


async def test_analytics_reports_corpus_totals_and_stage_throughput(
    make_client, session_factory
) -> None:
    prefix = uuid.uuid4().hex
    stage = f"probe-{prefix[:8]}"

    async with make_client() as client:
        before = (await client.get("/analytics")).json()

    doc_ids = await _seed_docs(session_factory, prefix)
    await _seed_runs(session_factory, stage, doc_ids)

    async with make_client() as client:
        response = await client.get("/analytics")
    assert response.status_code == 200
    after = response.json()

    assert after["corpus"]["document_count"] - before["corpus"]["document_count"] == 4
    # 4 rows, 3 doc_keys.
    assert after["corpus"]["doc_key_count"] - before["corpus"]["doc_key_count"] == 3
    # 10 + 20 + 30 + NULL-as-0.
    assert after["corpus"]["total_bytes"] - before["corpus"]["total_bytes"] == 60
    assert after["corpus"]["latest_ingested_at"] is not None

    # doc0's latest `index` run is done.
    assert after["documents_complete"] - before["documents_complete"] == 1
    # doc2's latest probe run is failed.
    assert after["documents_failed"] - before["documents_failed"] == 1

    probe = next(s for s in after["stages"] if s["name"] == stage)
    assert probe == {
        "name": stage,
        "done": 2,
        "running": 0,
        "queued": 0,
        "failed": 1,
        "median_seconds": 20.0,
        # This synthetic stage name never appears in ocr_usage_event/
        # llm_usage_event (costing feature) -- only "ocr" and "semantic"
        # ever bill, so every other stage's cost_inr is always 0.0.
        "cost_inr": 0.0,
    }
    names = [s["name"] for s in after["stages"]]
    assert names == sorted(names)


async def test_analytics_reports_null_median_for_a_stage_with_no_done_runs(
    make_client, session_factory
) -> None:
    from fdi.platform.db.models import StageRun

    prefix = uuid.uuid4().hex
    stage = f"probe-{prefix[:8]}"
    doc_ids = await _seed_docs(session_factory, prefix)
    async with session_factory() as s:
        s.add(
            StageRun(
                id=str(uuid.uuid4()),
                doc_id=doc_ids[0],
                stage_name=stage,
                stage_version=1,
                input_hash="h",
                status="running",
                created_at=_at(0),
                updated_at=_at(45),
            )
        )
        await s.commit()

    async with make_client() as client:
        body = (await client.get("/analytics")).json()

    probe = next(s for s in body["stages"] if s["name"] == stage)
    assert probe["running"] == 1
    assert probe["done"] == 0
    assert probe["median_seconds"] is None


async def test_analytics_chunk_count_reads_the_vector_store_not_postgres(make_client) -> None:
    """RT-9: the `chunk` table is gone; `chunk_count` now reads
    `VectorStore.count()`. No `FDI_MILVUS_URI` in `make_client`'s env means
    `app.state.vector_store` is None absent an override -- exercised first,
    then overridden with a `FakeVectorStore` holding a known number of rows.
    """
    async with make_client() as client:
        body = (await client.get("/analytics")).json()
    assert body["chunk_count"] == 0

    fake = FakeVectorStore()
    fake.upsert([
        {"id": "d1:s0", "vector": [1.0, 0.0], "metadata": {}},
        {"id": "d1:s1", "vector": [0.0, 1.0], "metadata": {}},
        {"id": "d2:s0", "vector": [1.0, 1.0], "metadata": {}},
    ])
    async with make_client(vector_store=fake) as client:
        body = (await client.get("/analytics")).json()
    assert body["chunk_count"] == 3


async def test_empty_corpus_returns_zeros_not_nulls(session_factory) -> None:
    """The aggregates on an empty corpus, read inside an uncommitted delete.

    Rolled back at the end, so the shared container database is untouched --
    but for the length of this transaction the session genuinely sees zero
    rows, which is the only way to reach this branch of the SQL.
    """
    from fdi.platform.db.models import Document, LlmUsageEvent, OcrUsageEvent, StageRun
    from fdi.platform.db.repository import DocumentRepository, RegistryRepository

    async with session_factory() as s:
        # ocr_usage_event/llm_usage_event FK to stage_run (costing feature),
        # which FKs to document -- children before parents, or the DELETE
        # FROM stage_run below violates ocr_usage_event_stage_run_id_fkey
        # once any test in this shared container database has recorded a
        # usage event.
        for model in (OcrUsageEvent, LlmUsageEvent, StageRun, Document):
            await s.execute(sqlalchemy.delete(model))

        totals = await DocumentRepository(s).corpus_totals()
        assert totals == {
            "document_count": 0,
            "doc_key_count": 0,
            "total_bytes": 0,
            "latest_ingested_at": None,
        }
        registry = RegistryRepository(s)
        assert await registry.stage_throughput() == []
        assert await registry.document_outcome_counts() == {"complete": 0, "failed": 0}

        await s.rollback()
