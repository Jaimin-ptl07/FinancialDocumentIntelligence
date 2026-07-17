import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.embedding import EmbeddingStage
from fdi.pipeline.stages.normalize import NormalizeStage
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


async def test_embedding_stage_runs_and_caches(  # fixtures from conftest
    session_factory, store
) -> None:
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
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Assets 100"},
            ],
            "tables": [],
        }
    )
    fake_embedder = FakeEmbeddingClient(dim=8)

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(EmbeddingStage())
    resolver = DagResolver(reg.all())

    assert "embedding" in resolver.successors("ast")  # the chain is automatic

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    ocr_services = {"ocr": fake_ocr}
    embed_services = {"embedder": fake_embedder}
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services=ocr_services, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()), services=embed_services, **common
    )
    # Second run: content-addressed cache hit -- must NOT call the fake embedder twice.
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()), services=embed_services, **common
    )
    assert fake_embedder.calls == 1

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    embedding_key = artifact_key(
        doc.id, "embedding", 1, derived_input_hash([facts["ast"].output_hash])
    )
    body = await store.get_json(embedding_key)
    assert body["model"] == "fake-embedding"
    assert body["dim"] == 8
    assert [c["chunk_id"] for c in body["chunks"]] == ["s0"]
    assert body["chunks"][0]["heading"] == "Balance Sheet"
    assert len(body["chunks"][0]["vector"]) == 8
