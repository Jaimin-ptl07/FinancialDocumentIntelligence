"""ChunksStage auto-chains off AstStage, SemanticStage, ProvenanceStage, AND (Phase
3a) StructureStage: `chunks` is the RT-1 retrieval foundation, joining a document's
section text (ast), section type (semantic), page (provenance), and table-derived
row/whole-table chunks (structure) into one per-document `chunks` record under the
shared `s{i}`/`t{i}`/`t{i}r{j}` chunk identities. Against real Postgres + MinIO with
FAKE OCR + semantic clients."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
from fdi.pipeline.ocr import FakeOcrClient
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
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


def _fake_ocr() -> FakeOcrClient:
    return FakeOcrClient(
        {
            "content": "ACME",
            "pages": [{"pageNumber": 1, "lines": [], "words": []}],
            "paragraphs": [
                {
                    "role": "title",
                    "content": "ACME LTD",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
                },
                {
                    "role": "sectionHeading",
                    "content": "Balance Sheet",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}],
                },
                {
                    "content": "Total Assets 100",
                    "boundingRegions": [{"pageNumber": 1, "polygon": [0, 2, 1, 2, 1, 3, 0, 3]}],
                },
                {
                    "role": "sectionHeading",
                    "content": "Directors' Report",
                    "boundingRegions": [{"pageNumber": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}],
                },
                {
                    "content": "The board is pleased.",
                    "boundingRegions": [{"pageNumber": 2, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}],
                },
            ],
            "tables": [
                {
                    "rowCount": 2,
                    "columnCount": 2,
                    "boundingRegions": [{"pageNumber": 1, "polygon": []}],
                    "spans": [{"offset": 100, "length": 10}],
                    "caption": {"content": "Balance Sheet"},
                    "cells": [
                        {"rowIndex": 0, "columnIndex": 0, "kind": "columnHeader",
                         "content": "Item"},
                        {"rowIndex": 0, "columnIndex": 1, "kind": "columnHeader",
                         "content": "Value"},
                        {"rowIndex": 1, "columnIndex": 0, "content": "Cash"},
                        {"rowIndex": 1, "columnIndex": 1, "content": "100"},
                    ],
                },
            ],
        }
    )


async def test_chunks_stage_produces_records(session_factory, store) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="a.pdf",
        doc_key=str(uuid.uuid4()),
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )

    fake_ocr = _fake_ocr()
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
    reg.register(GraphStage())
    reg.register(ProvenanceStage())
    reg.register(StructureStage())
    reg.register(ChunksStage())
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
        stage_name="ocr", run_id=str(uuid.uuid4()), services={"ocr": fake_ocr}, **common
    )
    await run_stage(stage_name="normalize", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="ast", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="semantic",
        run_id=str(uuid.uuid4()),
        services={"semantic": fake_semantic},
        **common,
    )
    await run_stage(stage_name="structure", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="provenance", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    chunks_key = artifact_key(
        doc.id,
        "chunks",
        2,
        derived_input_hash(
            [
                facts["ast"].output_hash,
                facts["semantic"].output_hash,
                facts["provenance"].output_hash,
                facts["structure"].output_hash,
            ]
        ),
    )
    body = await store.get_json(chunks_key)
    assert body == {
        "schema_version": 1,
        "chunks": [
            {
                "chunk_id": "s0",
                "heading": "Balance Sheet",
                "text": "Balance Sheet\nTotal Assets 100",
                "section_type": "BALANCE_SHEET",
                "page": 1,
                "para_count": 1,
            },
            {
                "chunk_id": "s1",
                "heading": "Directors' Report",
                "text": "Directors' Report\nThe board is pleased.",
                "section_type": "DIRECTORS_REPORT",
                "page": 2,
                "para_count": 1,
            },
            {
                "chunk_id": "t0r1",
                "heading": "Balance Sheet",
                "text": "Balance Sheet | Cash: Value=100",
                "section_type": None,
                "page": 1,
                "para_count": None,
            },
            {
                "chunk_id": "t0",
                "heading": "Balance Sheet",
                "text": "Balance Sheet\nValue\nCash: Value=100",
                "section_type": None,
                "page": 1,
                "para_count": None,
            },
        ],
    }


async def test_chunks_is_successor_of_all_four_deps() -> None:
    import fdi.pipeline.stages  # noqa: F401 - registers built-ins
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver as _DagResolver

    resolver = _DagResolver(REGISTRY.all())
    assert "chunks" in resolver.successors("ast")
    assert "chunks" in resolver.successors("semantic")
    assert "chunks" in resolver.successors("provenance")
    assert "chunks" in resolver.successors("structure")
