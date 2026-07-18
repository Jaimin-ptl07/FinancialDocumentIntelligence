"""keyword_search: plainto_tsquery + ts_rank over the `chunk` table. Against
real Postgres, seeded via index_chunks_fts over a processed_doc fixture."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.retrieval import index_chunks_fts, keyword_search
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
async def indexed_doc(session_factory, store) -> tuple[str, str, int]:
    """One processed + FTS-indexed document (Balance Sheet + Directors'
    Report chunks). Returns (doc_id, doc_key, version)."""
    doc_key = f"annual-{uuid.uuid4()}"
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
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
                {"content": "Goodwill impairment charge of 100",
                 "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}]},
                {"role": "sectionHeading", "content": "Directors' Report",
                 "boundingRegions": [{"pageNumber": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]},
                {"content": "The board is pleased with performance.",
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
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        await index_chunks_fts(session=s, store=store, doc_id=doc.id)
        await s.commit()
    return doc.id, doc_key, 1


async def test_keyword_search_ranks_the_matching_chunk_first(
    session_factory, indexed_doc
) -> None:
    doc_id, _, _ = indexed_doc
    async with session_factory() as s:
        hits = await keyword_search(session=s, query="goodwill impairment", k=5)
    ours = [h for h in hits if h["doc_id"] == doc_id]
    assert ours[0]["chunk_id"] == "s0"
    assert ours[0]["section_type"] == "BALANCE_SHEET"
    assert ours[0]["score"] > 0.0
    assert "Goodwill impairment" in ours[0]["text"]


async def test_keyword_search_filters_by_section_type(session_factory, indexed_doc) -> None:
    doc_id, _, _ = indexed_doc
    async with session_factory() as s:
        hits = await keyword_search(
            session=s, query="board", k=5, section_type="BALANCE_SHEET"
        )
    assert all(h["chunk_id"] != "s1" or h["doc_id"] != doc_id for h in hits)


async def test_keyword_search_filters_by_doc_key_and_version(session_factory, indexed_doc) -> None:
    doc_id, doc_key, version = indexed_doc
    async with session_factory() as s:
        hits = await keyword_search(
            session=s, query="board", k=5, doc_key=doc_key, version=version
        )
    assert any(h["doc_id"] == doc_id and h["chunk_id"] == "s1" for h in hits)
    async with session_factory() as s:
        none_hits = await keyword_search(
            session=s, query="board", k=5, doc_key="no-such-doc-key", version=1
        )
    assert none_hits == []


async def test_keyword_search_k_limits_results(session_factory, indexed_doc) -> None:
    async with session_factory() as s:
        hits = await keyword_search(session=s, query="the a", k=1)
    assert len(hits) <= 1


async def test_keyword_search_empty_query_returns_empty(session_factory, indexed_doc) -> None:
    async with session_factory() as s:
        hits = await keyword_search(session=s, query="", k=5)
    assert hits == []


async def test_keyword_search_hit_shape(session_factory, indexed_doc) -> None:
    doc_id, doc_key, version = indexed_doc
    # k is large, not 5: Postgres is session-scoped across this whole file (and
    # the wider integration suite), so earlier tests' fixtures already seeded
    # other documents with this exact same "goodwill" text -- an equal-score
    # tie means our row is not guaranteed a top-5 slot under an unspecified
    # tie-break order. A membership assertion over a wide-enough result set is
    # the same posture test_keyword_search_route_filters_by_section_type takes
    # for the same reason (see the RT-3 design doc's testing section).
    async with session_factory() as s:
        hits = await keyword_search(session=s, query="goodwill", k=1000)
    ours = next(h for h in hits if h["doc_id"] == doc_id)
    assert set(ours) == {
        "doc_id", "doc_key", "version", "chunk_id", "heading",
        "page", "section_type", "text", "score",
    }
    assert ours["doc_key"] == doc_key and ours["version"] == version
