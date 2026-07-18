"""fdi.pipeline.entities.compare_concept_across_versions against real
Postgres + MinIO, FAKE OCR. Reuses test_tables_lookup.py's numeric-table OCR
fixture pattern (structure only -- concept_lookup never reads `knowledge`),
intaking the SAME doc_key across MULTIPLE versions with DIFFERENT revenue
figures to prove the cross-version comparison the roadmap's "REVENUE across
FY2021->FY2023" example describes, AND a third version left un-structured to
exercise the None ("no_structure") vs [] ("no_match") distinction end to end
(design spec decision 3) -- not just at the pure-function level
(test_entities_comparison.py already covers that in isolation).
"""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.ontology import CanonicalConcept
from fdi.pipeline.entities import compare_concept_across_versions
from fdi.pipeline.intake import intake_document
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ocr import OcrStage
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


def _numeric_table_ocr(header: str, value: str) -> FakeOcrClient:
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
                {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader", "content": "Particulars"},
                {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader", "content": header},
                {"rowIndex": 1, "columnIndex": 0, "content": "Total revenue from operations"},
                {"rowIndex": 1, "columnIndex": 1, "content": value},
            ],
        }],
        "sections": [],
    })


async def _intake_and_structure(session_factory, store, doc_key, ocr) -> str:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(),
        filename="a.pdf", doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(StructureStage())
    resolver = DagResolver(reg.all())
    common = {
        "doc_id": doc.id, "correlation_id": "c1", "session_factory": session_factory,
        "store": store, "registry": reg, "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": ocr}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    return doc.id


async def _intake_unstructured(session_factory, store, doc_key) -> str:
    """A THIRD version of the same doc_key, intaken but never run through
    `structure` -- exercises concept_lookup's None ("no_structure") branch."""
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-{uuid.uuid4()}\n".encode(),
        filename="c.pdf", doc_key=doc_key, content_type="application/pdf",
        session_factory=session_factory, store=store,
    )
    return doc.id


@pytest.fixture
async def three_version_doc_key(session_factory, store) -> tuple[str, str, str, str]:
    """v1: FY24 revenue 12,345. v2: FY25 revenue 15,000. v3: intaken only, no
    `structure` artifact. Returns (doc_key, id_v1, id_v2, id_v3)."""
    doc_key = str(uuid.uuid4())
    id_v1 = await _intake_and_structure(
        session_factory, store, doc_key, _numeric_table_ocr("FY24", "12,345")
    )
    id_v2 = await _intake_and_structure(
        session_factory, store, doc_key, _numeric_table_ocr("FY25", "15,000")
    )
    id_v3 = await _intake_unstructured(session_factory, store, doc_key)
    return doc_key, id_v1, id_v2, id_v3


async def test_compare_revenue_across_versions(
    session_factory, store, three_version_doc_key
) -> None:
    doc_key, id_v1, id_v2, id_v3 = three_version_doc_key
    async with session_factory() as s:
        result = await compare_concept_across_versions(
            session=s, store=store, doc_key=doc_key, concept=CanonicalConcept.REVENUE
        )
    assert result is not None
    assert result["doc_key"] == doc_key
    assert result["concept"] == "REVENUE"
    versions = {v["doc_id"]: v for v in result["versions"]}

    assert versions[id_v1]["status"] == "matched"
    assert versions[id_v1]["rows"][0]["values"][0] == {
        "column_header": "FY24", "raw": "12,345", "value": 12345.0,
    }
    assert versions[id_v2]["status"] == "matched"
    assert versions[id_v2]["rows"][0]["values"][0] == {
        "column_header": "FY25", "raw": "15,000", "value": 15000.0,
    }
    assert versions[id_v3]["status"] == "no_structure"
    assert versions[id_v3]["rows"] == []


async def test_compare_concept_not_present_is_no_match(
    session_factory, store, three_version_doc_key
) -> None:
    doc_key, id_v1, id_v2, _id_v3 = three_version_doc_key
    async with session_factory() as s:
        result = await compare_concept_across_versions(
            session=s, store=store, doc_key=doc_key, concept=CanonicalConcept.EQUITY
        )
    assert result is not None
    versions = {v["doc_id"]: v for v in result["versions"]}
    assert versions[id_v1]["status"] == "no_match"
    assert versions[id_v2]["status"] == "no_match"


async def test_compare_concept_unknown_doc_key_returns_none(session_factory, store) -> None:
    async with session_factory() as s:
        result = await compare_concept_across_versions(
            session=s, store=store, doc_key="no-such-key", concept=CanonicalConcept.REVENUE
        )
    assert result is None
