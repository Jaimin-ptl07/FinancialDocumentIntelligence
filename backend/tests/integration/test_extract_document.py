"""extract_document end-to-end against real Postgres/MinIO with FakeOcr +
FakeSemantic + FakeExtractorClient -- NO embedder/vector_store configured
(hybrid_search degrades to keyword-only, RT-4) and NO Azure OpenAI call
anywhere in this file. Proves: a text field's pool comes from
keyword_search over the section chunk's FTS row; a numeric field's pool
ALSO comes from keyword_search alone -- via the table CARD (`t{i}`, TR-1 P2)
that build_chunks emits from the `structure` artifact's tables and
index_chunks_fts indexes right alongside section chunks, no separate
get_tables injection (a get_tables table-blob candidate was measured to
flood the rerank with same-scored whole-table renders and HURT top-20
recall; removed, see fdi.pipeline.extractor's module docstring); an
unmatchable, non-numeric field's pool is empty and still yields a
well-formed {value: None, confidence: 0.0} record (decision 10).

ExtractContext carries doc_id AND doc_key AND version (resolved once by the
caller -- here, the test itself; in production, the route) so extract_field
can scope hybrid_search by {doc_key, version} directly, without an O(fields)
per-field DocumentRepository.get lookup inside the loop."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.extractor import ExtractResult, FieldSpec, parse_field_specs
from fdi.core.pricing import LlmCallUsage
from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.extractor import (
    ExtractContext,
    FakeExtractorClient,
    extract_document,
    extract_unit,
)
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.semantic import FakeSemanticClient
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.basis import BasisStage
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


def _schema(marker: str) -> dict[str, object]:
    """v3.1.0-shaped: modules -> retrieval_units -> fields. `retrieval_query`
    is per UNIT, so the three fields need three units (the unmatchable one
    gets its own unit with a deliberately unmatchable query)."""
    return {
        "version": "3.1.0",
        "modules": [
            {
                "id": 1, "key": "module_1_overview", "name": "Overview",
                "retrieval_units": [
                    {
                        "unit_id": "RU_OVERVIEW",
                        "title": "Company Overview",
                        "scope": "front_matter",
                        "locator_keywords": ["manufacturing"],
                        "retrieval_query": f"manufacturing company {marker}",
                        "fields": [
                            {
                                "field_name": "Company overview text",
                                "aliases": ["overview"],
                                "data_type": "text",
                                "extraction_prompt": "Extract the company overview.",
                            },
                        ],
                    },
                ],
            },
            {
                "id": 2, "key": "module_2_balance_sheet", "name": "Balance Sheet",
                "retrieval_units": [
                    {
                        "unit_id": "RU_BS",
                        "title": "Balance Sheet",
                        "scope": "primary_statement",
                        "statement_type": "BS",
                        "locator_keywords": ["total", "assets"],
                        "retrieval_query": "Total assets",
                        "fields": [
                            {
                                "field_name": "Total assets",
                                "aliases": ["Total assets"],
                                "data_type": "decimal",
                                "extraction_prompt": "Extract the total assets value.",
                            },
                        ],
                    },
                    {
                        "unit_id": "RU_NOWHERE",
                        "title": "Nowhere",
                        "scope": "note",
                        "locator_keywords": [],
                        "retrieval_query": "zzzznonexistenttoken9999 impossible query",
                        "fields": [
                            {
                                "field_name": "Unmatchable field",
                                "aliases": [],
                                "data_type": "string",
                                "extraction_prompt": "Extract nothing findable.",
                            },
                        ],
                    },
                ],
            },
        ],
    }


@pytest.fixture
async def processed_doc(session_factory, store):
    """One fully-processed document: a text section (matched by BM25/vector
    search over its s{i} chunk) + a numeric table (matched by BM25/vector
    search over its t{i} table CARD, indexed into Milvus right alongside
    section chunks -- no get_tables involved). RT-9: the pool now comes from
    a REAL (fake) Milvus, so `embedding` and `index` are run too, injecting a
    FakeEmbeddingClient/FakeVectorStore -- mirroring
    test_index_enrichment.py's pattern -- and the fakes are returned so the
    caller can build an ExtractContext with a populated store. Returns
    (doc, marker, embedder, vector_store)."""
    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), ChunksStage(), StructureStage(), BasisStage(),
                  EmbeddingStage(), IndexStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()
    fake_ocr = FakeOcrClient({
        "content": "ACME LTD",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {
                "role": "sectionHeading", "content": "Overview",
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
            },
            {
                "content": f"ACME Ltd is a manufacturing company {marker}.",
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}],
            },
        ],
        "tables": [
            {
                "rowCount": 2, "columnCount": 2,
                "spans": [{"offset": 20, "length": 60}],
                "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                     "content": "Particulars"},
                    {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": "FY24"},
                    {"rowIndex": 1, "columnIndex": 0, "content": "Total assets"},
                    {"rowIndex": 1, "columnIndex": 1, "content": "12,345"},
                ],
            },
        ],
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
    await run_stage(stage_name="basis", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()),
        services={"embedder": embedder}, **common,
    )
    await run_stage(
        stage_name="index", run_id=str(uuid.uuid4()),
        services={"vector_store": vector_store}, **common,
    )
    return doc, marker, embedder, vector_store


async def test_extract_document_pools_keyword_candidates_only(
    session_factory, store, processed_doc
) -> None:
    doc, marker, embedder, vector_store = processed_doc
    fields = parse_field_specs(_schema(marker))
    assert len(fields) == 3  # both modules' fields, no module filter

    fake = FakeExtractorClient([
        {"Company overview text": {
            "value": "ACME is a manufacturer.",
            "source": {"page": 1, "snippet": "ACME Ltd is a manufacturing company",
                       "evidence_ids": [f"{doc.id}:s0"]},
            "confidence": 0.9}},
        {"Total assets": {
            "value": 12345.0,
            "source": {"page": None, "snippet": "Total assets 12,345",
                       "evidence_ids": [f"{doc.id}:t0"]},
            "confidence": 0.95}},
        # RU_NOWHERE IS retrieved -- the vector channel always returns its
        # k nearest neighbours, so a hybrid pool is never empty for a
        # non-empty corpus (unlike the old plainto_tsquery channel, which
        # returned nothing on a no-match query). Its unit reaches the client
        # -- three calls, not two -- and the model returns null for context
        # it cannot answer. This is why every one of the 18 production units
        # always costs one LLM call.
        {"Unmatchable field": {
            "value": None,
            "source": {"page": None, "snippet": None, "evidence_ids": []},
            "confidence": 0.0}},
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        result = await extract_document(ctx=ctx, fields=fields, client=fake)

    assert result["doc_id"] == doc.id
    assert result["field_count"] == 3
    f0, f1, f2 = result["fields"]

    assert f0["field_name"] == "Company overview text"
    assert f0["value"] == "ACME is a manufacturer."
    assert f0["confidence"] == 0.9

    assert f1["field_name"] == "Total assets"
    assert f1["value"] == 12345.0
    assert f1["source"]["evidence_ids"] == [f"{doc.id}:t0"]

    assert f2["field_name"] == "Unmatchable field"
    assert f2["value"] is None
    assert f2["confidence"] == 0.0
    # The model cited nothing (evidence_ids stays the [] it returned), so
    # there is nothing to highlight. page/snippet are NOT asserted None:
    # with a non-empty pool, assemble_result's decision-8 fallback fills
    # them from the top candidate.
    assert f2["source"]["evidence_ids"] == []

    # B4: geometry rides along on `source`, joined from the provenance
    # (s{i} -> graph node -> OCR paragraph) and structure (t{i} -> the
    # whole table's bounding region) artifacts. {current, prior}, not a flat
    # list (2026-07-25 cell-precise design) -- `prior` stays [] here since
    # this fixture's table has no per-cell boundingRegions to resolve
    # (processed_doc's fake OCR cells carry none), so `current` falls back to
    # resolve_evidence_regions's whole-table/whole-section box exactly as
    # before, just wrapped in the new shape.
    assert isinstance(f0["source"]["bounding_regions"]["current"], list)
    assert f0["source"]["bounding_regions"]["prior"] == []
    assert f1["source"]["bounding_regions"] == {
        "current": [{"page_number": 1, "polygon": []}], "prior": [],
    }
    # The unmatchable field cited nothing, so there is nothing to highlight --
    # [] rather than null, and never an exception (decision 10).
    assert f2["source"]["bounding_regions"] == {"current": [], "prior": []}

    # One LLM call per Retrieval Unit -- THREE, because the vector channel
    # gives even RU_NOWHERE a pool. The numeric unit's prompt carries the
    # table CARD rendered as a MARKDOWN TABLE (TR-1 P3-wire): the ranked
    # card's grid is fetched from the `structure` artifact and rendered with
    # `render_table_markdown`/`repair_headers`, replacing the P2 LOCATION-
    # summary text ("Columns: .../Rows: ...", no cell values) with real
    # headers + real cell VALUES ("12,345" now reaches the prompt). No
    # get_tables candidate (evidence_id would be "table:...") appears
    # anywhere.
    assert len(fake.calls) == 3
    for _system, _user in fake.calls:
        assert "table:" not in _user
    table_call_user = next(
        _user for _system, _user in fake.calls if "| --- | --- |" in _user
    )
    assert "| Particulars | FY24 |" in table_call_user
    assert "| Total assets | 12,345 |" in table_call_user


async def test_extract_field_blank_query_pool_is_empty_and_well_formed(
    session_factory, store, processed_doc
) -> None:
    """spec sec 4: an empty/whitespace retrieval_query -> empty pool ->
    value None. parse_field_specs never produces such a FieldSpec (it
    rejects blank retrieval_query at parse time, see Task 1) -- this
    exercises extract_field's OWN defensive guard directly, for a
    hand-built FieldSpec bypassing the parser."""
    from fdi.core.extractor import FieldSpec
    from fdi.pipeline.extractor import extract_field

    doc, _, _, _ = processed_doc
    blank = FieldSpec(
        field_name="Blank query field", data_type="string", retrieval_query="   ",
        keywords=(), extraction_prompt="x", enum_values=None, module_id=9,
        module_name="X", module_key="m9", unit_id="RU_X", unit_title="X",
        scope=None, statement_type=None,
    )
    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=None, vector_store=None,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        out = await extract_field(ctx=ctx, field=blank, client=FakeExtractorClient([]))
    # extract_field is the pre-enrichment half: bounding_regions is attached
    # by extract_document, which is what reads the two artifacts.
    # `tag`/`origin` ride on every record since the Fact model (M1): a fact says
    # where it came from even when it came from nowhere.
    assert out == {
        "field_name": "Blank query field", "value": None, "tag": "",
        "origin": "extracted",
        "source": {"page": None, "snippet": None, "evidence_ids": []}, "confidence": 0.0,
    }


async def test_extract_document_makes_one_llm_call_per_unit(
    session_factory, store, processed_doc
) -> None:
    """THE batching regression: 5 fields across 2 units must produce 2
    client.extract calls, not 5 -- and come back in INPUT order."""
    doc, marker, embedder, vector_store = processed_doc

    def _f(name: str, unit_id: str) -> FieldSpec:
        return FieldSpec(
            field_name=name, data_type="string",
            retrieval_query=f"manufacturing company {marker}",
            keywords=(name,), extraction_prompt=f"Extract {name}.", enum_values=None,
            module_id=1, module_name="M", unit_id=unit_id, unit_title=f"T-{unit_id}",
            field_path=f"m.{name}", locator_keywords=("manufacturing",),
        )

    # interleaved on purpose: input order (a,b,c,d,e) != unit order (a,b,e,c,d)
    fields = [
        _f("a", "U1"), _f("b", "U1"), _f("c", "U2"), _f("d", "U2"), _f("e", "U1"),
    ]
    fake = FakeExtractorClient([
        {"m.a": {"value": "A", "confidence": 1.0},
         "m.b": {"value": "B", "confidence": 1.0},
         "m.e": {"value": "E", "confidence": 1.0}},
        {"m.c": {"value": "C", "confidence": 1.0},
         "m.d": {"value": "D", "confidence": 1.0}},
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        result = await extract_document(ctx=ctx, fields=fields, client=fake)

    assert len(fake.calls) == 2, f"expected 2 unit calls, got {len(fake.calls)}"
    assert result["field_count"] == 5
    # input order preserved, NOT unit order
    assert [r["field_name"] for r in result["fields"]] == ["a", "b", "c", "d", "e"]
    assert [r["value"] for r in result["fields"]] == ["A", "B", "C", "D", "E"]


async def test_extract_document_second_call_is_a_cache_hit(
    session_factory, store, processed_doc
) -> None:
    """A second call for the same (doc_id, schema) must NOT call the LLM
    again -- it serves the record the first call already persisted. FakeExtractorClient
    raises AssertionError on an over-call, so this only passes if the cache
    check short-circuits before any retrieval/LLM work."""
    doc, marker, embedder, vector_store = processed_doc

    field = FieldSpec(
        field_name="a", data_type="string",
        retrieval_query=f"manufacturing company {marker}",
        keywords=("a",), extraction_prompt="Extract a.", enum_values=None,
        module_id=1, module_name="M", unit_id="U1", unit_title="T-U1",
        field_path="m.a", locator_keywords=("manufacturing",),
    )
    fake = FakeExtractorClient([
        {"m.a": {"value": "A", "confidence": 1.0}},
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        first = await extract_document(ctx=ctx, fields=[field], client=fake)
        second = await extract_document(ctx=ctx, fields=[field], client=fake)

    assert len(fake.calls) == 1, "second call must be a cache hit, not a re-extraction"
    assert second["fields"] == first["fields"]


async def test_extract_document_force_bypasses_cache(
    session_factory, store, processed_doc
) -> None:
    """`force=True` re-runs extraction even when a cached record exists."""
    doc, marker, embedder, vector_store = processed_doc

    field = FieldSpec(
        field_name="a", data_type="string",
        retrieval_query=f"manufacturing company {marker}",
        keywords=("a",), extraction_prompt="Extract a.", enum_values=None,
        module_id=1, module_name="M", unit_id="U1", unit_title="T-U1",
        field_path="m.a", locator_keywords=("manufacturing",),
    )
    fake = FakeExtractorClient([
        {"m.a": {"value": "A", "confidence": 1.0}},
        {"m.a": {"value": "A2", "confidence": 1.0}},
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        await extract_document(ctx=ctx, fields=[field], client=fake)
        second = await extract_document(ctx=ctx, fields=[field], client=fake, force=True)

    assert len(fake.calls) == 2
    assert second["fields"][0]["value"] == "A2"


async def test_extract_document_basis_filter_drops_consolidated_table_for_standalone_unit(
    session_factory, store,
) -> None:
    """TR-1 P4b: a unit's DEFAULT `basis_preference` ("standalone") drops
    table candidates the `basis` stage tags "consolidated" -- replacing the
    old single-page `consolidated_start_page` cutoff with the labeled
    basis-region model (design spec sec 6.2). Two SA-700 auditor's-report
    anchors split this document into a standalone region (page 1) and a
    consolidated region (page 50); a near-identical "Total assets" table
    sits in each region with a DIFFERENT value (12,345 vs 99,999). The
    standalone unit's (default basis_preference) prompt must carry the
    standalone table's rendered value and must NEVER carry the consolidated
    table's -- proving the basis filter, not just the render wiring."""
    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), ChunksStage(), StructureStage(), BasisStage(),
                  EmbeddingStage(), IndexStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()

    fake_ocr = FakeOcrClient({
        "content": "ACME LTD",
        "pages": [
            {"pageNumber": 1, "lines": [], "words": []},
            {"pageNumber": 50, "lines": [], "words": []},
        ],
        "paragraphs": [
            {
                "role": "sectionHeading",
                "content": (
                    "Independent Auditor's Report on the Standalone "
                    "Financial Statements"
                ),
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
            },
            {
                "content": f"ACME Ltd standalone commentary {marker}.",
                "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}],
            },
            {
                "role": "sectionHeading",
                "content": (
                    "Independent Auditor's Report on the Consolidated "
                    "Financial Statements"
                ),
                "boundingRegions": [{"pageNumber": 50, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
            },
            {
                "content": f"ACME Ltd consolidated commentary {marker}.",
                "boundingRegions": [{"pageNumber": 50, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}],
            },
        ],
        "tables": [
            {
                "rowCount": 2, "columnCount": 2,
                "spans": [{"offset": 20, "length": 60}],
                "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                     "content": "Particulars"},
                    {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": "FY24"},
                    {"rowIndex": 1, "columnIndex": 0, "content": "Total assets"},
                    {"rowIndex": 1, "columnIndex": 1, "content": "12,345"},
                ],
            },
            {
                "rowCount": 2, "columnCount": 2,
                "spans": [{"offset": 200, "length": 60}],
                "boundingRegions": [{"pageNumber": 50, "polygon": []}],
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                     "content": "Particulars"},
                    {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": "FY24"},
                    {"rowIndex": 1, "columnIndex": 0, "content": "Total assets"},
                    {"rowIndex": 1, "columnIndex": 1, "content": "99,999"},
                ],
            },
        ],
    })

    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
        doc_key=f"acme-basis-{marker}", content_type="application/pdf",
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
        services={"semantic": FakeSemanticClient({})}, **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="basis", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()),
        services={"embedder": embedder}, **common,
    )
    await run_stage(
        stage_name="index", run_id=str(uuid.uuid4()),
        services={"vector_store": vector_store}, **common,
    )

    # Sanity: the basis stage actually split standalone/consolidated regions
    # -- otherwise this test would pass for the wrong reason (nothing to
    # filter).
    async with session_factory() as s:
        run = await RegistryRepository(s).latest_run(doc.id, "basis")
        basis_body = await store.get_json(run.artifact_key)
    regions = basis_body["regions"]
    assert any(r["basis"] == "standalone" for r in regions)
    assert any(r["basis"] == "consolidated" for r in regions)

    field = FieldSpec(
        field_name="Total assets", data_type="decimal",
        retrieval_query="Total assets", keywords=("Total assets",),
        extraction_prompt="Extract the total assets value.", enum_values=None,
        module_id=1, module_name="Balance Sheet", unit_id="RU_BS", unit_title="Balance Sheet",
        field_path="m.total_assets", locator_keywords=("total", "assets"),
        # basis_preference omitted -> defaults to "standalone"
    )
    fake = FakeExtractorClient([
        {"m.total_assets": {
            "value": 12345.0,
            "source": {"page": 1, "snippet": "Total assets 12,345", "evidence_ids": []},
            "confidence": 0.95}},
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        result = await extract_document(ctx=ctx, fields=[field], client=fake)

    assert result["field_count"] == 1
    assert len(fake.calls) == 1
    _system, user = fake.calls[0]
    assert "12,345" in user
    assert "99,999" not in user


async def test_extract_document_gives_table_field_a_cell_precise_box(
    session_factory, store,
) -> None:
    """A numeric field whose keywords match a table row must get a
    bounding_regions shape of {current, prior}, with current/prior each
    pointing at their OWN cell's geometry -- distinct from each other and
    from the table's own whole-table box -- proving the cell-level lookup
    ran, not the old whole-table fallback. Data cells here carry their own
    `boundingRegions` (unlike processed_doc's shared fixture, whose fake
    table has none), which is what makes cell-level geometry reachable at
    all -- see fdi.core.ocr_schema's per-cell boundingRegions normalization."""
    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), ChunksStage(), StructureStage(), BasisStage(),
                  EmbeddingStage(), IndexStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    embedder = FakeEmbeddingClient(dim=8)
    vector_store = FakeVectorStore()

    fake_ocr = FakeOcrClient({
        "content": "ACME LTD",
        "pages": [{"pageNumber": 79, "lines": [], "words": []}],
        "paragraphs": [
            {
                "role": "sectionHeading", "content": "Balance Sheet",
                "boundingRegions": [{"pageNumber": 79, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
            },
            {
                "content": f"ACME Ltd manufacturing company {marker}.",
                "boundingRegions": [{"pageNumber": 79, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}],
            },
        ],
        "tables": [
            {
                "rowCount": 2, "columnCount": 3,
                "spans": [{"offset": 20, "length": 60}],
                "boundingRegions": [{"pageNumber": 79, "polygon": [0, 0, 10, 0, 10, 10, 0, 10]}],
                "cells": [
                    {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                     "content": "Particulars"},
                    {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader",
                     "content": "Column 1 (most recent)"},
                    {"rowIndex": 0, "columnIndex": 2, "kind": "columnHeader",
                     "content": "Column 2 (prior)"},
                    {"rowIndex": 1, "columnIndex": 0,
                     "content": "Property, plant & Equipments",
                     "boundingRegions": [{"pageNumber": 79, "polygon": [0, 1, 3, 1, 3, 2, 0, 2]}]},
                    {"rowIndex": 1, "columnIndex": 1, "content": "2018.29",
                     "boundingRegions": [{"pageNumber": 79, "polygon": [3, 1, 6, 1, 6, 2, 3, 2]}]},
                    {"rowIndex": 1, "columnIndex": 2, "content": "1838.80",
                     "boundingRegions": [{"pageNumber": 79, "polygon": [6, 1, 9, 1, 9, 2, 6, 2]}]},
                ],
            },
        ],
    })

    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(), filename="a.pdf",
        doc_key=f"acme-cellbox-{marker}", content_type="application/pdf",
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
        services={"semantic": FakeSemanticClient({})}, **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="basis", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()),
        services={"embedder": embedder}, **common,
    )
    await run_stage(
        stage_name="index", run_id=str(uuid.uuid4()),
        services={"vector_store": vector_store}, **common,
    )

    field = FieldSpec(
        field_name="Property plant equipment net", data_type="decimal",
        retrieval_query=f"manufacturing company {marker}",
        keywords=("property", "plant", "equipments"),
        extraction_prompt="Extract property plant and equipment net.",
        enum_values=None, module_id=1, module_name="M", unit_id="U1",
        unit_title="T-U1", field_path="m.ppe", periods=("current", "prior"),
        locator_keywords=("total", "assets"),
    )
    fake = FakeExtractorClient([
        {"m.ppe": {
            "value": {"current": 2018.29, "prior": 1838.80},
            "confidence": 1.0,
            "source": {"evidence_ids": ["t0"]},
        }},
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        result = await extract_document(ctx=ctx, fields=[field], client=fake)

    bounding_regions = result["fields"][0]["source"]["bounding_regions"]
    assert set(bounding_regions.keys()) == {"current", "prior"}
    assert bounding_regions["current"], "expected cell-precise current-period geometry"
    assert bounding_regions["prior"], "expected cell-precise prior-period geometry"
    assert bounding_regions["current"] != bounding_regions["prior"]

    # cell-precise, not the whole table: the table's own box spans the full
    # [0,0]-[10,10] polygon; the matched cell's box is a narrow strip of it.
    current_polygon = bounding_regions["current"][0]["polygon"]
    assert current_polygon == [3, 1, 6, 1, 6, 2, 3, 2]
    prior_polygon = bounding_regions["prior"][0]["polygon"]
    assert prior_polygon == [6, 1, 9, 1, 9, 2, 6, 2]


class _UsageCarryingExtractorClient:
    """Local test double -- unlike FakeExtractorClient, returns a real
    usage on every call, to prove extract_unit records it (costing
    feature, docs/superpowers/specs/2026-07-28-costing-feature-design.md)."""

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def extract(self, *, system: str, user: str) -> ExtractResult:
        return ExtractResult(
            data=self._data,
            usage=LlmCallUsage(
                model="gpt-4o", input_tokens=80, output_tokens=15, duration_ms=200
            ),
        )


async def test_extract_unit_records_usage_when_client_returns_it(
    session_factory, store, processed_doc
) -> None:
    """extract_unit's `_record_llm_usage` call (wired in a prior task, wired
    to an actual implementation here) writes one llm_usage_event row,
    source="extract_document", carrying the real usage a client returns --
    proven end to end via RegistryRepository.document_cost_by_source, the
    same read path GET /analytics/documents/{doc_id}/cost will use."""
    from fdi.core.extractor import UnitSpec

    doc, marker, embedder, vector_store = processed_doc
    field = FieldSpec(
        field_name="a", data_type="string",
        retrieval_query=f"manufacturing company {marker}",
        keywords=("a",), extraction_prompt="Extract a.", enum_values=None,
        module_id=1, module_name="M", unit_id="U1", unit_title="T-U1",
        field_path="m.a", locator_keywords=("manufacturing",),
    )
    unit = UnitSpec(
        unit_id="U1", unit_title="T-U1",
        retrieval_query=f"manufacturing company {marker}",
        locator_keywords=("manufacturing",), fields=(field,),
    )
    client = _UsageCarryingExtractorClient({"value": "100", "confidence": 0.9})

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        await extract_unit(ctx=ctx, unit=unit, client=client)
        await s.commit()

    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc.id)
    row = next(r for r in rows if r["source"] == "extract_document")
    assert row["input_tokens"] == 80
    assert row["output_tokens"] == 15


async def test_extract_document_records_no_usage_for_plain_fake_client(
    session_factory, store, processed_doc
) -> None:
    """FakeExtractorClient's default `usage=None` (used everywhere else in
    this file) must record NOTHING -- extract_unit only calls
    _record_llm_usage when `result.usage is not None`. Guards against a
    regression that would record a bogus usage event, and INR-cost, for
    every one of this file's other (usage-free) tests."""
    doc, marker, embedder, vector_store = processed_doc
    fields = parse_field_specs(_schema(marker))

    fake = FakeExtractorClient([
        {"Company overview text": {"value": "ACME is a manufacturer.", "confidence": 0.9}},
        {"Total assets": {"value": 12345.0, "confidence": 0.95}},
        {"Unmatchable field": {"value": None, "confidence": 0.0}},
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=embedder, vector_store=vector_store,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        await extract_document(ctx=ctx, fields=fields, client=fake)
        await s.commit()

    async with session_factory() as s:
        rows = await RegistryRepository(s).document_cost_by_source(doc.id)
    assert not any(r["source"] == "extract_document" for r in rows)
