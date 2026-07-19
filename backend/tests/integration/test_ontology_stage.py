"""OntologyStage auto-chains off AstStage AND StructureStage: `ontology` is a
DERIVED stage (`deps=("ast", "structure")`, Phase 4) that maps every AST
section heading/paragraph to a CanonicalConcept via the pure
dictionary-based map_document AND every structure table grid's numeric rows
to CanonicalConcept values via map_concept_values, merging both into ONE
artifact. Against real Postgres + MinIO with a FAKE OCR client."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.ast import AstStage
from fdi.pipeline.stages.normalize import NormalizeStage
from fdi.pipeline.stages.ocr import OcrStage
from fdi.pipeline.stages.ontology import OntologyStage
from fdi.pipeline.stages.structure import StructureStage
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


def _numeric_table_ocr() -> FakeOcrClient:
    """ONE table carrying a numeric data row -- the codebase currently has no
    such fixture reused across tests (RT-6's own note); built inline here,
    same approach RT-6's test_tables_lookup.py took."""
    return FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Revenue"},
                {"content": "Sales"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Total Assets"},
            ],
            "tables": [
                {
                    "rowCount": 2,
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
                    ],
                },
            ],
        }
    )


async def test_ontology_chains_off_ast(session_factory, store) -> None:  # fixtures from conftest
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )

    fake = FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Revenue"},
                {"content": "Sales"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Total Assets"},
            ],
            "tables": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(OntologyStage())
    resolver = DagResolver(reg.all())

    assert "ontology" in resolver.successors("ast")  # the chain is automatic
    assert "ontology" in resolver.successors("structure")  # NEW: second dep also schedules ontology

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ontology", run_id=str(uuid.uuid4()), services={}, **common)
    # Second run: content-addressed cache hit against the same ast/structure output_hashes.
    await run_stage(stage_name="ontology", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    ontology_key = artifact_key(
        doc.id, "ontology", 2,
        derived_input_hash([facts["ast"].output_hash, facts["structure"].output_hash]),
    )
    body = await store.get_json(ontology_key)
    assert body["schema_version"] == 2
    assert body["mappings"] == [
        {"label": "Revenue", "concept": "REVENUE"},
        {"label": "Sales", "concept": "REVENUE"},
        {"label": "Total Assets", "concept": "TOTAL_ASSETS"},
    ]
    assert body["concept_values"] == []  # no tables in this fixture


async def test_ontology_concept_values_populated_from_structure_tables(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="b.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(OntologyStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id,
        "correlation_id": "c2",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(
        stage_name="ocr", run_id=str(uuid.uuid4()),
        services={"ocr": _numeric_table_ocr()}, **common,
    )
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ontology", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    ontology_key = artifact_key(
        doc.id, "ontology", 2,
        derived_input_hash([facts["ast"].output_hash, facts["structure"].output_hash]),
    )
    body = await store.get_json(ontology_key)
    assert body["mappings"]  # non-empty, from the AST-label path (unchanged)
    concept_values = body["concept_values"]
    assert concept_values  # non-empty, from the new structure path
    revenue_entries = [e for e in concept_values if e["concept"] == "REVENUE"]
    assert revenue_entries[0]["row_label"] == "Total revenue from operations"
    assert revenue_entries[0]["value"] == 12345.0
