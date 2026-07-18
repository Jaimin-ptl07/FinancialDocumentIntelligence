"""IndexStage v2 auto-chains off BOTH EmbeddingStage and ChunksStage: the
upserted Milvus metadata now carries `section_type` + `page` sourced from the
RT-1 `chunks` artifact, in addition to SP-14's `heading` + `chunk_id`. Against
real Postgres + MinIO with FAKE OCR + semantic + embedding clients and a
FakeVectorStore that records every upserted item."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.embeddings import FakeEmbeddingClient
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
from fdi.pipeline.stages.index import IndexStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.provenance import ProvenanceStage
from fdi.pipeline.stages.semantic import SemanticStage
from fdi.pipeline.stages.structure import StructureStage
from fdi.pipeline.vectorstore import FakeVectorStore
from fdi.platform.db.engine import make_session_factory
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


def _registry() -> StageRegistry:
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(SemanticStage())
    reg.register(GraphStage())
    reg.register(ProvenanceStage())
    reg.register(StructureStage())
    reg.register(ChunksStage())
    reg.register(EmbeddingStage())
    reg.register(IndexStage())
    return reg


async def test_index_upserts_enriched_metadata(session_factory, store) -> None:
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
                {
                    "role": "title",
                    "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
                },
                {
                    "role": "sectionHeading",
                    "content": "Balance Sheet",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}],
                },
                {
                    "content": "Assets 100",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}],
                },
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient({"Balance Sheet": SectionType.BALANCE_SHEET.value})
    fake_embedder = FakeEmbeddingClient(dim=8)
    fake_store = FakeVectorStore()

    reg = _registry()
    resolver = DagResolver(reg.all())

    assert "index" in resolver.successors("embedding")
    assert "index" in resolver.successors("chunks")

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
    await run_stage(
        stage_name="semantic",
        run_id=str(uuid.uuid4()),
        services={"semantic": fake_semantic},
        **common,
    )
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding",
        run_id=str(uuid.uuid4()),
        services={"embedder": fake_embedder},
        **common,
    )
    await run_stage(
        stage_name="index",
        run_id=str(uuid.uuid4()),
        services={"vector_store": fake_store},
        **common,
    )

    query_vector = fake_embedder.embed(["Balance Sheet\nAssets 100"])[0]
    hits = fake_store.search(query_vector, k=1)
    assert hits[0]["id"] == f"{doc.id}:s0"
    assert hits[0]["metadata"] == {
        "heading": "Balance Sheet",
        "chunk_id": "s0",
        "section_type": "BALANCE_SHEET",
        "page": 1,
    }


async def test_index_is_successor_of_embedding_and_chunks() -> None:
    import fdi.pipeline.stages  # noqa: F401 - registers built-ins
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver as _DagResolver

    resolver = _DagResolver(REGISTRY.all())
    assert "index" in resolver.successors("embedding")
    assert "index" in resolver.successors("chunks")
