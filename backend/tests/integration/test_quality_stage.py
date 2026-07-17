# backend/tests/integration/test_quality_stage.py
"""QualityStage auto-chains off AstStage AND StructureStage: `quality` is a
DERIVED stage (`deps=("ast","structure")`) that validates the naive AST for
internal defects AND the structure artifact's tables for shape defects,
merging both into ONE report via `validate_all`. Against real Postgres +
MinIO with a FAKE OCR client. (SP-17b: the pipeline's first
two-dependency stage.)"""

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
from fdi.pipeline.stages.quality import QualityStage
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


async def test_quality_chains_off_ast_and_structure(session_factory, store) -> None:
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
                {"role": "sectionHeading", "content": "Directors' Report"},
                {"content": "The board is pleased."},
            ],
            "tables": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(QualityStage())
    resolver = DagResolver(reg.all())

    assert "quality" in resolver.successors("ast")  # the chain is automatic
    assert "quality" in resolver.successors("structure")  # NEW: second dep also schedules quality

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
    await run_stage(stage_name="quality", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    quality_key = artifact_key(
        doc.id, "quality", 2,
        derived_input_hash([facts["ast"].output_hash, facts["structure"].output_hash]),
    )
    body = await store.get_json(quality_key)
    assert body == {
        "schema_version": 1,
        "issues": [],
        "error_count": 0,
        "warning_count": 0,
        "ok": True,
    }


async def test_quality_merges_ast_and_structure_defects_into_one_report(
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

    # No title, no heading, no paragraphs -> normalize/ast produce an empty AST.
    # ONE table whose second cell's column_index (2) >= its column_count (2) -> a torn grid.
    fake = FakeOcrClient(
        {
            "content": "",
            "pages": [],
            "paragraphs": [],
            "tables": [
                {
                    "rowCount": 1,
                    "columnCount": 2,
                    "cells": [
                        {"rowIndex": 0, "columnIndex": 0, "content": "Q1"},
                        {"rowIndex": 0, "columnIndex": 2, "content": "Q2"},
                    ],
                },
            ],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(QualityStage())
    resolver = DagResolver(reg.all())

    common = {
        "doc_id": doc.id,
        "correlation_id": "c2",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake}, **common)
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="quality", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    quality_key = artifact_key(
        doc.id, "quality", 2,
        derived_input_hash([facts["ast"].output_hash, facts["structure"].output_hash]),
    )
    body = await store.get_json(quality_key)

    codes = [issue["code"] for issue in body["issues"]]
    assert codes == ["ast.no_sections", "ast.missing_title", "structure.torn_table"]
    assert body["error_count"] == 1  # only ast.no_sections is an error
    assert body["warning_count"] == 2  # ast.missing_title + structure.torn_table
    assert body["ok"] is False
