"""fdi.pipeline.graphquery: neighbors/document_structure/reading_order, each
citation-enriched via the `provenance` artifact + the Document row. Against
real Postgres + MinIO with FAKE OCR/semantic clients and a FakeGraphStore."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.graphquery import document_structure, neighbors, reading_order
from fdi.pipeline.graphstore import FakeGraphStore
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.graphload import GraphLoadStage
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
async def graph_store() -> FakeGraphStore:
    return FakeGraphStore()


@pytest.fixture
async def processed_doc(session_factory, store, graph_store: FakeGraphStore) -> str:
    """Intake one FakeOcr document and run ocr -> ... -> graph_load."""
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key="ACME-10K", content_type="application/pdf",
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
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(SemanticStage())
    reg.register(StructureStage())
    reg.register(GraphStage())
    reg.register(ProvenanceStage())
    reg.register(GraphLoadStage())
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
    await run_stage(
        stage_name="graph_load", run_id=str(uuid.uuid4()),
        services={"graph_store": graph_store}, **common,
    )
    return doc.id


async def test_neighbors_returns_cited_hits(
    session_factory, store, graph_store: FakeGraphStore, processed_doc: str
) -> None:
    async with session_factory() as s:
        out = await neighbors(
            session=s, store=store, graph_store=graph_store, node_id=f"{processed_doc}:doc"
        )
    assert out["node_id"] == f"{processed_doc}:doc"
    section = next(n for n in out["neighbors"] if n["node"]["id"] == f"{processed_doc}:s0")
    assert section["relationship"] == "CONTAINS"
    assert section["direction"] == "out"
    citation = section["node"]["citation"]
    assert citation["doc_id"] == processed_doc
    assert citation["doc_key"] == "ACME-10K"
    assert citation["page"] == 1


async def test_neighbors_malformed_node_id_returns_empty(
    session_factory, store, graph_store: FakeGraphStore
) -> None:
    async with session_factory() as s:
        out = await neighbors(session=s, store=store, graph_store=graph_store, node_id="no-colon")
    assert out["neighbors"] == []


async def test_document_structure_returns_ordered_cited_tree(
    session_factory, store, graph_store: FakeGraphStore, processed_doc: str
) -> None:
    async with session_factory() as s:
        out = await document_structure(
            session=s, store=store, graph_store=graph_store, doc_id=processed_doc
        )
    assert out["doc_id"] == processed_doc
    section_labels = [s["label"] for s in out["sections"]]
    assert section_labels == ["Balance Sheet", "Directors' Report"]
    first_section = out["sections"][0]
    assert first_section["citation"]["page"] == 1
    assert [p["label"] for p in first_section["paragraphs"]] == ["Total Assets 100"]
    assert first_section["paragraphs"][0]["citation"]["page"] == 1


async def test_document_structure_unknown_doc_id(session_factory, store, graph_store) -> None:
    async with session_factory() as s:
        out = await document_structure(
            session=s, store=store, graph_store=graph_store, doc_id="no-such-doc"
        )
    assert out == {"schema_version": 1, "doc_id": "no-such-doc", "document": None, "sections": []}


async def test_reading_order_matches_document_structure_section_order(
    session_factory, store, graph_store: FakeGraphStore, processed_doc: str
) -> None:
    async with session_factory() as s:
        out = await reading_order(
            session=s, store=store, graph_store=graph_store, doc_id=processed_doc
        )
    assert [s["label"] for s in out["sections"]] == ["Balance Sheet", "Directors' Report"]
    assert all("paragraphs" not in s for s in out["sections"])
