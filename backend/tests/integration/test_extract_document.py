"""extract_document end-to-end against real Postgres/MinIO with FakeOcr +
FakeSemantic + FakeExtractorClient -- NO embedder/vector_store configured
(hybrid_search degrades to keyword-only, RT-4) and NO Azure OpenAI call
anywhere in this file. Proves: a text field's pool comes from
keyword_search over the section chunk's FTS row; a numeric field's pool
ALSO comes from keyword_search alone -- via the table-row chunk (t{i}r{j})
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

from fdi.core.extractor import parse_field_specs
from fdi.pipeline.extractor import ExtractContext, FakeExtractorClient, extract_document
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
    return {
        "modules": [
            {
                "id": 1, "name": "OVERVIEW", "folder": "1-overview",
                "fields": [
                    {
                        "field_name": "Company overview text",
                        "source_type": "document_content", "rag_extractable": True,
                        "data_type": "text",
                        "retrieval_query": f"manufacturing company {marker}",
                        "extraction_prompt": "Extract the company overview.",
                        "keywords": ["manufacturing"],
                    },
                ],
            },
            {
                "id": 2, "name": "BALANCE_SHEET", "folder": "2-balance-sheet",
                "fields": [
                    {
                        "field_name": "Total assets",
                        "source_type": "document_content", "rag_extractable": True,
                        "data_type": "decimal", "retrieval_query": "Total assets",
                        "extraction_prompt": "Extract the total assets value.",
                        "keywords": ["total", "assets"],
                    },
                    {
                        "field_name": "Unmatchable field",
                        "source_type": "document_content", "rag_extractable": True,
                        "data_type": "string",
                        "retrieval_query": "zzzznonexistenttoken9999 impossible query",
                        "extraction_prompt": "Extract nothing findable.", "keywords": [],
                    },
                ],
            },
        ],
    }


@pytest.fixture
async def processed_doc(session_factory, store):
    """One fully-processed document: a text section (matched by keyword
    search over its s{i} chunk) + a numeric table (matched by keyword
    search over its t{i}/t{i}r{j} table chunks, indexed into chunk FTS
    right alongside section chunks -- no get_tables involved). Returns
    (doc, marker)."""
    marker = uuid.uuid4().hex
    reg = StageRegistry()
    for stage in (OcrStage(), NormalizeStage(), AstStage(), SemanticStage(), GraphStage(),
                  ProvenanceStage(), ChunksStage(), StructureStage()):
        reg.register(stage)
    resolver = DagResolver(reg.all())
    fake_ocr = FakeOcrClient({
        "content": "ACME LTD",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
        "paragraphs": [
            {"role": "sectionHeading", "content": "Overview"},
            {"content": f"ACME Ltd is a manufacturing company {marker}."},
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
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    async with session_factory() as s:
        await index_chunks_fts(session=s, store=store, doc_id=doc.id)
        await s.commit()
    return doc, marker


async def test_extract_document_pools_keyword_candidates_only(
    session_factory, store, processed_doc
) -> None:
    doc, marker = processed_doc
    fields = parse_field_specs(_schema(marker))
    assert len(fields) == 3  # both modules' fields, no module filter

    fake = FakeExtractorClient([
        {"value": "ACME is a manufacturer.",
         "source": {"page": 1, "snippet": "ACME Ltd is a manufacturing company",
                     "evidence_ids": [f"{doc.id}:s0"]},
         "confidence": 0.9},
        {"value": 12345.0,
         "source": {"page": None, "snippet": "Total assets 12,345",
                     "evidence_ids": [f"{doc.id}:t0r1"]},
         "confidence": 0.95},
        # The third field's pool is empty -- extract_field short-circuits
        # BEFORE calling the client, so only 2 scripted results are consumed.
    ])

    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=None, vector_store=None,
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
    assert f1["source"]["evidence_ids"] == [f"{doc.id}:t0r1"]

    assert f2["field_name"] == "Unmatchable field"
    assert f2["value"] is None
    assert f2["confidence"] == 0.0
    assert f2["source"] == {"page": None, "snippet": None, "evidence_ids": []}

    # Exactly 2 real LLM calls (the third field never reaches the client);
    # the numeric field's prompt carries the table row chunk's text --
    # found by keyword_search over the chunk FTS index, no get_tables
    # candidate (evidence_id would be "table:...") anywhere.
    assert len(fake.calls) == 2
    assert "table:" not in fake.calls[0][1]
    assert "table:" not in fake.calls[1][1]
    assert "12,345" in fake.calls[1][1]


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

    doc, _ = processed_doc
    blank = FieldSpec(
        field_name="Blank query field", data_type="string", retrieval_query="   ",
        keywords=(), extraction_prompt="x", enum_values=None, module_id=9, module_name="X",
    )
    async with session_factory() as s:
        ctx = ExtractContext(
            session=s, store=store, embedder=None, vector_store=None,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
        )
        out = await extract_field(ctx=ctx, field=blank, client=FakeExtractorClient([]))
    assert out == {
        "field_name": "Blank query field", "value": None,
        "source": {"page": None, "snippet": None, "evidence_ids": []}, "confidence": 0.0,
    }
