"""index_chunks_fts: upsert a document's RT-1 `chunks` artifact into the
`chunk` table. Against real Postgres + MinIO with FAKE OCR + semantic
clients -- reuses test_resolve_chunk.py's `processed_doc` fixture verbatim."""

import uuid

import pytest
import sqlalchemy
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.retrieval import index_chunks_fts
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
from fdi.platform.db.repository import DocumentRepository
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
    """Intake one FakeOcr document and run ocr -> ... -> chunks. Returns
    doc.id. Identical fixture to test_resolve_chunk.py's -- two chunks,
    BALANCE_SHEET (page 1) and DIRECTORS_REPORT (page 2)."""
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
                {"role": "title", "content": "ACME LTD",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"role": "sectionHeading", "content": "Balance Sheet",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]},
                {"content": "Total Assets 100",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}]},
                {"role": "sectionHeading", "content": "Directors' Report",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"content": "The board is pleased.",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]},
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
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(),
                  GraphStage(), ProvenanceStage(), StructureStage(), ChunksStage()):
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
    await run_stage(stage_name="semantic", run_id=str(uuid.uuid4()),
                     services={"semantic": fake_semantic}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    return doc.id


async def _chunk_rows(session_factory, doc_id: str) -> list[sqlalchemy.Row]:
    async with session_factory() as s:
        result = await s.execute(
            sqlalchemy.text(
                "SELECT id, doc_id, chunk_id, doc_key, version, heading, text, "
                "section_type, page FROM chunk WHERE doc_id = :d ORDER BY chunk_id"
            ),
            {"d": doc_id},
        )
        return result.all()


async def test_index_chunks_fts_writes_rows_with_doc_key_and_version(
    session_factory, store, processed_doc
) -> None:
    async with session_factory() as s:
        doc = await DocumentRepository(s).get(processed_doc)
        count = await index_chunks_fts(session=s, store=store, doc_id=processed_doc)
        await s.commit()
    assert count == 2

    rows = await _chunk_rows(session_factory, processed_doc)
    assert [r.chunk_id for r in rows] == ["s0", "s1"]
    assert rows[0].id == f"{processed_doc}:s0"
    assert rows[0].doc_key == doc.doc_key and rows[0].version == doc.version
    assert rows[0].section_type == "BALANCE_SHEET" and rows[0].page == 1
    assert "Balance Sheet" in rows[0].text


async def test_index_chunks_fts_is_idempotent(session_factory, store, processed_doc) -> None:
    async with session_factory() as s:
        await index_chunks_fts(session=s, store=store, doc_id=processed_doc)
        await s.commit()
    async with session_factory() as s:
        count2 = await index_chunks_fts(session=s, store=store, doc_id=processed_doc)
        await s.commit()
    assert count2 == 2
    rows = await _chunk_rows(session_factory, processed_doc)
    assert len(rows) == 2  # no duplicates


async def test_index_chunks_fts_unknown_document_returns_zero(session_factory, store) -> None:
    async with session_factory() as s:
        count = await index_chunks_fts(session=s, store=store, doc_id="no-such-doc")
        await s.commit()
    assert count == 0


async def test_index_chunks_fts_no_chunks_artifact_returns_zero(session_factory, store) -> None:
    async with session_factory() as s:
        doc = await DocumentRepository(s).create(
            id=str(uuid.uuid4()), checksum=str(uuid.uuid4()), filename="x.pdf",
            byte_size=1, content_type="application/pdf", doc_key="k", version=1,
        )
        await s.commit()
        count = await index_chunks_fts(session=s, store=store, doc_id=doc.id)
        await s.commit()
    assert count == 0
