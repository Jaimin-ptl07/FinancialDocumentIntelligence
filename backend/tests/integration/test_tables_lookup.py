"""fdi.pipeline.tables.get_tables/concept_lookup: read a document's `structure`
artifact on demand (no new stage/artifact) and expose its canonicalized table
grids + concept->row matches. Against real Postgres + MinIO with a FAKE DI
client.

Pre-check finding: every existing FakeOcrClient table fixture in this suite
(test_structure_stage.py, test_chunks_stage.py, test_api.py's
resolved_corpus_via_api, ...) is header-only or has `"cells": []` -- none
carries a numeric data row. `_numeric_table_ocr()` below is a NEW, LOCAL
fixture built for this test; it does not repoint any shared fixture other
stage tests pin exact-equality assertions against.
"""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.ontology import CanonicalConcept
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.structure import StructureStage
from fdi.pipeline.tables import concept_lookup, get_tables
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
    return FakeOcrClient(
        {
            "content": "ACME LTD",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {
                    "role": "title",
                    "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 0, "length": 8}],
                },
            ],
            "tables": [
                {
                    "rowCount": 3,
                    "columnCount": 2,
                    "spans": [{"offset": 20, "length": 60}],
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "cells": [
                        {
                            "rowIndex": 0, "columnIndex": 0,
                            "kind": "columnHeader", "content": "Particulars",
                        },
                        {
                            "rowIndex": 0, "columnIndex": 1,
                            "kind": "columnHeader", "content": "FY24",
                        },
                        {
                            "rowIndex": 1, "columnIndex": 0,
                            "content": "Total revenue from operations",
                        },
                        {"rowIndex": 1, "columnIndex": 1, "content": "12,345"},
                        {"rowIndex": 2, "columnIndex": 0, "content": "Net profit"},
                        {"rowIndex": 2, "columnIndex": 1, "content": "(500)"},
                    ],
                },
            ],
            "sections": [],
        }
    )


@pytest.fixture
async def structured_doc(session_factory, store) -> str:
    """Intake one FakeOcr document (numeric table) and run ocr -> structure.
    Returns doc.id."""
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(StructureStage())
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
        stage_name="ocr", run_id=str(uuid.uuid4()),
        services={"ocr": _numeric_table_ocr()}, **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    return doc.id


async def test_get_tables_returns_tagged_tables(session_factory, store, structured_doc) -> None:
    async with session_factory() as s:
        tables = await get_tables(session=s, store=store, doc_id=structured_doc)
    assert tables is not None and len(tables) == 1
    assert tables[0]["table_index"] == 0
    assert tables[0]["grid"]["column_headers"] == ["Particulars", "FY24"]


async def test_get_tables_keyword_filter(session_factory, store, structured_doc) -> None:
    async with session_factory() as s:
        hit = await get_tables(session=s, store=store, doc_id=structured_doc, keyword="fy24")
        miss = await get_tables(session=s, store=store, doc_id=structured_doc, keyword="fy99")
    assert hit is not None and len(hit) == 1
    assert miss == []


async def test_get_tables_unknown_doc_returns_none(session_factory, store) -> None:
    async with session_factory() as s:
        assert await get_tables(session=s, store=store, doc_id="no-such-doc") is None


async def test_concept_lookup_revenue_and_net_profit(
    session_factory, store, structured_doc
) -> None:
    async with session_factory() as s:
        revenue = await concept_lookup(
            session=s, store=store, doc_id=structured_doc, concept=CanonicalConcept.REVENUE
        )
        net_profit = await concept_lookup(
            session=s, store=store, doc_id=structured_doc, concept=CanonicalConcept.NET_PROFIT
        )
    assert revenue == [{
        "table_index": 0,
        "row_label": "Total revenue from operations",
        "values": [{"column_header": "FY24", "raw": "12,345", "value": 12345.0}],
    }]
    assert net_profit == [{
        "table_index": 0,
        "row_label": "Net profit",
        "values": [{"column_header": "FY24", "raw": "(500)", "value": -500.0}],
    }]


async def test_concept_lookup_no_match_returns_empty_list(
    session_factory, store, structured_doc
) -> None:
    async with session_factory() as s:
        result = await concept_lookup(
            session=s, store=store, doc_id=structured_doc, concept=CanonicalConcept.EQUITY
        )
    assert result == []  # document HAS structure, just no matching row -- not None


async def test_concept_lookup_unknown_doc_returns_none(session_factory, store) -> None:
    async with session_factory() as s:
        result = await concept_lookup(
            session=s, store=store, doc_id="no-such-doc", concept=CanonicalConcept.REVENUE
        )
    assert result is None
