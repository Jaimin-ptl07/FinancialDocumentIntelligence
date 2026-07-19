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
        doc.id, "structure", 4, derived_input_hash([facts["ocr"].output_hash])
    )
    body = await store.get_json(structure_key)
    assert body["schema_version"] == 3
    assert len(body["tables"]) == 1 and body["tables"][0]["row_count"] == 5  # 2-page table stitched
    assert [b["role"] for b in body["blocks"]] == ["title"]  # footer removed
    assert body["tables"][0]["source_indices"] == [0, 1]  # raw ocr["tables"] indices, in order

    grid = body["tables"][0]["grid"]
    assert grid["n_rows"] == 5 and grid["n_cols"] == 3
    assert grid["header_row_count"] == 1
    assert grid["column_headers"] == ["Year", "Q1", "Q2"]
    assert grid["cell_regions"][0][0] == []  # no bounding_regions on these fixture cells
    assert grid["cell_pages"][0][0] is None


async def test_structure_merges_realistic_furniture_gap_end_to_end(  # fixtures from conftest
    session_factory, store
) -> None:
    """Phase 5: a real running-header/page-number furniture span (~29 chars,
    mirroring the audit's measured "\n60\nAnnual Report 2023-2024\n") between
    two adjacent-page, equal-column tables with no header on the second --
    the common genuine-continuation shape -- now stitches end-to-end."""
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="b.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )

    fake = FakeOcrClient(
        {
            "content": "x" * 200,
            "pages": [{"pageNumber": 1}, {"pageNumber": 2}],
            "paragraphs": [
                {
                    "role": "pageFooter",
                    "content": "\n60\nAnnual Report 2023-2024\n",
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 50, "length": 29}],
                },
            ],
            "tables": [
                {
                    "rowCount": 2,
                    "columnCount": 3,
                    "spans": [{"offset": 0, "length": 50}],
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "cells": [
                        {"rowIndex": 0, "columnIndex": 0,
                         "kind": "columnHeader", "content": "Year"},
                        {"rowIndex": 0, "columnIndex": 1,
                         "kind": "columnHeader", "content": "Q1"},
                        {"rowIndex": 0, "columnIndex": 2,
                         "kind": "columnHeader", "content": "Q2"},
                    ],
                },
                {
                    "rowCount": 3,
                    "columnCount": 3,
                    "spans": [{"offset": 79, "length": 40}],
                    "boundingRegions": [{"pageNumber": 2, "polygon": []}],
                    "cells": [],  # no header cells of its own -- genuine continuation
                },
            ],
            "sections": [],
        }
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
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    structure_key = artifact_key(
        doc.id, "structure", 4, derived_input_hash([facts["ocr"].output_hash])
    )
    body = await store.get_json(structure_key)
    assert len(body["tables"]) == 1  # merged into one stitched entry
    assert body["tables"][0]["row_count"] == 5
    assert body["tables"][0]["source_indices"] == [0, 1]


async def test_structure_blocks_fixed_assets_shaped_false_merge_end_to_end(  # fixtures below
    session_factory, store
) -> None:
    """Phase 5: same furniture-only-gap, equal-column shape, but table B
    carries its own header cells whose resolved column_headers differ from
    A's (the Note 2 Fixed-Assets current/prior-year shape) -- must stay two
    separate structure["tables"] entries end-to-end."""
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="c.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )

    fake = FakeOcrClient(
        {
            "content": "x" * 200,
            "pages": [{"pageNumber": 1}, {"pageNumber": 2}],
            "paragraphs": [
                {
                    "role": "pageFooter",
                    "content": "PREVIOUS YEAR DETAILS",  # mis-tagged as furniture by Azure
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 50, "length": 29}],
                },
            ],
            "tables": [
                {
                    "rowCount": 2,
                    "columnCount": 3,
                    "spans": [{"offset": 0, "length": 50}],
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "cells": [
                        {"rowIndex": 0, "columnIndex": 0,
                         "kind": "columnHeader", "content": "Year"},
                        {"rowIndex": 0, "columnIndex": 1,
                         "kind": "columnHeader", "content": "Q1"},
                        {"rowIndex": 0, "columnIndex": 2,
                         "kind": "columnHeader", "content": "Q2"},
                    ],
                },
                {
                    "rowCount": 3,
                    "columnCount": 3,
                    "spans": [{"offset": 79, "length": 40}],
                    "boundingRegions": [{"pageNumber": 2, "polygon": []}],
                    "cells": [
                        {"rowIndex": 0, "columnIndex": 0,
                         "kind": "columnHeader", "content": "Prior Yr"},
                        {"rowIndex": 0, "columnIndex": 1,
                         "kind": "columnHeader", "content": "H1"},
                        {"rowIndex": 0, "columnIndex": 2,
                         "kind": "columnHeader", "content": "H2"},
                    ],
                },
            ],
            "sections": [],
        }
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
    await run_stage(stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake}, **common)
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    structure_key = artifact_key(
        doc.id, "structure", 4, derived_input_hash([facts["ocr"].output_hash])
    )
    body = await store.get_json(structure_key)
    assert len(body["tables"]) == 2  # false merge stays blocked
    assert body["tables"][0]["source_indices"] == [0]
    assert body["tables"][1]["source_indices"] == [1]
