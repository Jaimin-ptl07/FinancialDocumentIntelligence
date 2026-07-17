"""StructureStage auto-chains off OcrStage: `structure` is a DERIVED stage
(`deps=("ocr",)`) that removes header/footer/page-number paragraphs and
stitches cross-page tables, against real Postgres + MinIO with a FAKE DI
client."""

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
from fdi.pipeline.stages.ocr import OcrStage
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


async def test_structure_stitches_and_filters(  # fixtures from conftest
    session_factory, store
) -> None:
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
            "content": "ACME LTD\nSchedule 1\nfoot\nSchedule 1 cont",
            "pages": [{"pageNumber": 1}, {"pageNumber": 2}],
            "paragraphs": [
                {
                    "role": "title",
                    "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 0, "length": 8}],
                },
                {
                    "role": "pageFooter",
                    "content": "foot",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 19, "length": 4}],
                },
            ],
            "tables": [
                {
                    "rowCount": 2,
                    "columnCount": 3,
                    "spans": [{"offset": 9, "length": 10}],
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "cells": [
                        {
                            "rowIndex": 0, "columnIndex": 0,
                            "kind": "columnHeader", "content": "Year",
                        },
                        {
                            "rowIndex": 0, "columnIndex": 1,
                            "kind": "columnHeader", "content": "Q1",
                        },
                        {
                            "rowIndex": 0, "columnIndex": 2,
                            "kind": "columnHeader", "content": "Q2",
                        },
                    ],
                },
                {
                    "rowCount": 3,
                    "columnCount": 3,
                    "spans": [{"offset": 21, "length": 14}],
                    "boundingRegions": [{"pageNumber": 2, "polygon": []}],
                    "cells": [],
                },
            ],
            "sections": [],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(StructureStage())
    resolver = DagResolver(reg.all())

    assert "structure" in resolver.successors("ocr")  # the chain is automatic

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    structure_key = artifact_key(
        doc.id, "structure", 2, derived_input_hash([facts["ocr"].output_hash])
    )
    body = await store.get_json(structure_key)
    assert len(body["tables"]) == 1 and body["tables"][0]["row_count"] == 5  # 2-page table stitched
    assert [b["role"] for b in body["blocks"]] == ["title"]  # footer removed

    grid = body["tables"][0]["grid"]
    assert grid["n_rows"] == 5 and grid["n_cols"] == 3
    assert grid["header_row_count"] == 1
    assert grid["column_headers"] == ["Year", "Q1", "Q2"]
