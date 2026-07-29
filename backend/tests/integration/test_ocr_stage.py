"""OcrStage over the intake'd PDF, against real Postgres + MinIO. A FAKE DI client
proves the wiring end to end, including that a cache hit does NOT call Azure again."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, root_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


async def test_ocr_stage_runs_and_caches(session_factory, store) -> None:  # fixtures from conftest
    doc = await intake_document(
        pdf=b"%PDF-1.7\n%fake-pdf-body\n",
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )

    fake = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [],
            "tables": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id,
        "stage_name": "ocr",
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
        "services": {"ocr": fake},
    }
    await run_stage(run_id=str(uuid.uuid4()), **common)
    await run_stage(run_id=str(uuid.uuid4()), **common)  # same input + version -> cache hit

    assert fake.calls == 1, "cache hit must NOT call Azure DI a second time"

    key = artifact_key(doc.id, "ocr", 5, root_input_hash(doc.checksum.encode()))
    body = await store.get_json(key)
    assert body["content"] == "ACME"
    assert body["schema_version"] == 5


async def test_ocr_stage_records_one_usage_event_per_real_call(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=b"%PDF-1.7\n%fake-pdf-body-2\n",
        filename="b.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    fake = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [
                {"pageNumber": 1, "lines": [], "words": []},
                {"pageNumber": 2, "lines": [], "words": []},
                {"pageNumber": 3, "lines": [], "words": []},
            ],
            "paragraphs": [],
            "tables": [],
        }
    )
    reg = StageRegistry()
    reg.register(OcrStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id, "stage_name": "ocr", "correlation_id": "c1",
        "session_factory": session_factory, "store": store,
        "registry": reg, "resolver": resolver, "services": {"ocr": fake},
    }
    await run_stage(run_id=str(uuid.uuid4()), **common)
    await run_stage(run_id=str(uuid.uuid4()), **common)  # cache hit -- must NOT bill again

    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc.id)
    ocr_row = next(r for r in rows if r["source"] == "ocr")
    assert ocr_row["call_count"] == 1, "cache hit must not create a second usage event"
    assert ocr_row["page_count"] == 3
