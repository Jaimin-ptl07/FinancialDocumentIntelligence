"""resolve_chunk/resolve_chunks: turn a `{doc_id}:{chunk_id}` hit id into its
full RT-1 chunk record (text + citation) by reading the document's `chunks`
artifact. Against real Postgres + MinIO with FAKE OCR + semantic clients."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.retrieval import resolve_chunk, resolve_chunks
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


@pytest.fixture
async def processed_doc(session_factory, store) -> str:
    """Intake one FakeOcr document and run ocr -> ... -> chunks. Returns doc.id."""
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
                    "content": "Total Assets 100",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}],
                },
                {
                    "role": "sectionHeading",
                    "content": "Directors' Report",
                    "boundingRegions": [{"pageNumber": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
                },
                {
                    "content": "The board is pleased.",
                    "boundingRegions": [{"pageNumber": 2, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}],
                },
            ],
            "tables": [],
        }
    )
    fake_semantic = FakeSemanticClient(
        {
            "Balance Sheet": SectionType.BALANCE_SHEET.value,
            "Directors' Report": SectionType.DIRECTORS_REPORT.value,
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(SemanticStage())
    reg.register(GraphStage())
    reg.register(ProvenanceStage())
    reg.register(StructureStage())
    reg.register(ChunksStage())
    resolver = DagResolver(reg.all())

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
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)

    return doc.id


async def test_resolve_chunk_returns_record_with_doc_id(
    session_factory, store, processed_doc
) -> None:
    async with session_factory() as s:
        rec = await resolve_chunk(session=s, store=store, doc_id=processed_doc, chunk_id="s0")
    assert rec is not None
    assert rec["doc_id"] == processed_doc and rec["chunk_id"] == "s0"
    assert "text" in rec and "section_type" in rec and "page" in rec


async def test_resolve_chunk_unknown_returns_none(session_factory, store, processed_doc) -> None:
    async with session_factory() as s:
        assert (
            await resolve_chunk(session=s, store=store, doc_id=processed_doc, chunk_id="s999")
            is None
        )
        assert (
            await resolve_chunk(session=s, store=store, doc_id="no-such-doc", chunk_id="s0")
            is None
        )


async def test_resolve_chunks_batch(session_factory, store, processed_doc) -> None:
    ids = [
        f"{processed_doc}:s0",
        f"{processed_doc}:s1",
        "bogus-no-colon",
        f"{processed_doc}:s999",
    ]
    async with session_factory() as s:
        out = await resolve_chunks(session=s, store=store, ids=ids)
    assert f"{processed_doc}:s0" in out and out[f"{processed_doc}:s0"]["doc_id"] == processed_doc
    assert "bogus-no-colon" not in out and f"{processed_doc}:s999" not in out
