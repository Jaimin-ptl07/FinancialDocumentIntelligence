"""execute_tool against real Postgres/MinIO with Fake embedder/vector_store/
graph_store -- no live Azure/Milvus/Neo4j. Reuses fixture patterns from
test_hybrid_search.py (search), test_tables_lookup.py (get_tables/
concept_lookup), test_graphquery.py (document_structure), test_resolve_
entities.py (resolve_entities), test_compare_concept.py/test_entity_
timeline.py (entity_timeline/compare_concept_across_versions)."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.agent_tools import AgentContext, execute_tool
from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.graphstore import FakeGraphStore
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
from fdi.pipeline.stages.knowledge import KnowledgeStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.ontology import OntologyStage
from fdi.pipeline.stages.provenance import ProvenanceStage
from fdi.pipeline.stages.quality import QualityStage
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


def _numeric_table_ocr() -> FakeOcrClient:
    return FakeOcrClient({
        "content": "ACME LTD",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "title", "content": "ACME LTD",
             "boundingRegions": [{"pageNumber": 1, "polygon": []}],
             "spans": [{"offset": 0, "length": 8}]},
        ],
        "tables": [{
            "rowCount": 2, "columnCount": 2,
            "spans": [{"offset": 20, "length": 60}],
            "boundingRegions": [{"pageNumber": 1, "polygon": []}],
            "cells": [
                {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                 "content": "Particulars"},
                {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": "FY24"},
                {"rowIndex": 1, "columnIndex": 0, "content": "Total revenue from operations"},
                {"rowIndex": 1, "columnIndex": 1, "content": "12,345"},
            ],
        }],
        "sections": [],
    })


# --------------------------------------------------------------------------
# Error-path tests -- only need execute_tool + AgentContext.
# --------------------------------------------------------------------------


async def test_unknown_tool_returns_structured_error(session_factory, store) -> None:
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("not_a_real_tool", {}, ctx=ctx)
    assert out == {"error": "unknown tool 'not_a_real_tool'"}


async def test_bad_arguments_returns_structured_error(session_factory, store) -> None:
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("search", {}, ctx=ctx)  # missing required "query"
    assert "error" in out and "search" in out["error"]


async def test_document_structure_without_graph_store_returns_structured_error(
    session_factory, store
) -> None:
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("document_structure", {"doc_id": "d1"}, ctx=ctx)
    assert out == {"error": "document_structure is not configured: missing graph_store"}


async def test_get_tables_no_structure_artifact_yet_returns_structured_error(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf", session_factory=session_factory, store=store,
    )
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("get_tables", {"doc_id": doc.id}, ctx=ctx)
    assert "error" in out


async def test_entity_timeline_unknown_doc_key_returns_structured_error(
    session_factory, store
) -> None:
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("entity_timeline", {"doc_key": "no-such-key"}, ctx=ctx)
    assert "error" in out


# --------------------------------------------------------------------------
# Happy-path tests -- one per registered tool.
# --------------------------------------------------------------------------


async def test_search_happy_path(session_factory, store) -> None:
    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), ChunksStage(), EmbeddingStage(), IndexStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()

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
    sem_map = {"Balance Sheet": SectionType.BALANCE_SHEET.value}
    await run_stage(
        stage_name="semantic", run_id=str(uuid.uuid4()),
        services={"semantic": FakeSemanticClient(sem_map)},
        **common,
    )
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()),
        services={"embedder": embedder}, **common,
    )
    await run_stage(
        stage_name="index", run_id=str(uuid.uuid4()),
        services={"vector_store": vector_store}, **common,
    )

    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=embedder, vector_store=vector_store,
                            graph_store=None)
        out = await execute_tool(
            "search", {"query": f"Total Assets 100 {marker}"}, ctx=ctx
        )
    assert "results" in out
    assert out["results"]
    assert out["results"][0]["doc_id"] == doc.id


async def test_document_structure_happy_path(session_factory, store) -> None:
    graph_store = FakeGraphStore()
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
        doc_key=str(uuid.uuid4()), content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    graph_store.load(
        [
            {"id": f"{doc.id}:doc", "type": "document", "label": "ACME", "doc_id": doc.id},
            {"id": f"{doc.id}:s0", "type": "section", "label": "Balance Sheet", "doc_id": doc.id},
        ],
        [{"source": f"{doc.id}:doc", "target": f"{doc.id}:s0", "type": "CONTAINS"}],
    )
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=graph_store)
        out = await execute_tool("document_structure", {"doc_id": doc.id}, ctx=ctx)
    assert "error" not in out
    assert out["doc_id"] == doc.id
    assert out["sections"][0]["id"] == f"{doc.id}:s0"


async def test_get_tables_happy_path(session_factory, store) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
        doc_key=str(uuid.uuid4()), content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(StructureStage())
    resolver = DagResolver(reg.all())
    common = {"doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
              "store": store, "registry": reg, "resolver": resolver}
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": _numeric_table_ocr()},
        **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("get_tables", {"doc_id": doc.id}, ctx=ctx)
    assert "tables" in out
    assert out["tables"][0]["grid"]["column_headers"] == ["Particulars", "FY24"]


async def test_concept_lookup_happy_path(session_factory, store) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
        doc_key=str(uuid.uuid4()), content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(StructureStage())
    resolver = DagResolver(reg.all())
    common = {"doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
              "store": store, "registry": reg, "resolver": resolver}
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": _numeric_table_ocr()},
        **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool(
            "concept_lookup", {"doc_id": doc.id, "concept": "REVENUE"}, ctx=ctx
        )
    assert "rows" in out
    assert out["rows"][0]["row_label"] == "Total revenue from operations"


async def test_resolve_entities_happy_path(session_factory, store) -> None:
    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("resolve_entities", {}, ctx=ctx)
    assert "entities" in out
    assert isinstance(out["entities"], list)


async def _run_entity_timeline_registry() -> tuple[StageRegistry, DagResolver]:
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(SemanticStage())
    reg.register(OntologyStage())
    reg.register(QualityStage())
    reg.register(KnowledgeStage())
    return reg, DagResolver(reg.all())


async def test_entity_timeline_happy_path(session_factory, store) -> None:
    doc_key = str(uuid.uuid4())
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
        doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    reg, resolver = await _run_entity_timeline_registry()
    fake_ocr = FakeOcrClient({
        "content": "ACME",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "title", "content": "ACME"},
            {"role": "sectionHeading", "content": "Balance Sheet"},
            {"content": "Total Assets"},
        ],
        "tables": [],
    })
    common = {"doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
              "store": store, "registry": reg, "resolver": resolver}
    sem_map = {"Balance Sheet": SectionType.BALANCE_SHEET.value}
    for stage, services in (
        ("ocr", {"ocr": fake_ocr}), ("normalize", {}), ("ast", {}), ("structure", {}),
        ("semantic", {"semantic": FakeSemanticClient(sem_map)}),
        ("ontology", {}), ("quality", {}), ("knowledge", {}),
    ):
        await run_stage(stage_name=stage, run_id=str(uuid.uuid4()), services=services, **common)

    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool("entity_timeline", {"doc_key": doc_key}, ctx=ctx)
    assert "timeline" in out
    assert out["timeline"][0]["doc_id"] == doc.id


async def test_compare_concept_across_versions_happy_path(session_factory, store) -> None:
    doc_key = str(uuid.uuid4())
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
        doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(StructureStage())
    resolver = DagResolver(reg.all())
    common = {"doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
              "store": store, "registry": reg, "resolver": resolver}
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": _numeric_table_ocr()},
        **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        ctx = AgentContext(session=s, store=store, embedder=None, vector_store=None,
                            graph_store=None)
        out = await execute_tool(
            "compare_concept_across_versions", {"doc_key": doc_key, "concept": "REVENUE"}, ctx=ctx
        )
    assert "versions" in out
    assert out["versions"][0]["doc_id"] == doc.id
    assert out["versions"][0]["status"] == "matched"
