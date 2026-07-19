"""GraphStage auto-chains off AstStage AND StructureStage: `graph` is a
DERIVED stage (`deps=("ast", "structure")`) that builds the CONTAINS/
READING_ORDER graph from the naive AST plus table/figure nodes from
`structure`, against real Postgres + MinIO with a FAKE OCR client."""

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
from fdi.pipeline.stages.graph import GraphStage
from fdi.pipeline.stages.normalize import NormalizeStage
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


async def test_graph_chains_off_ast_and_structure(session_factory, store) -> None:
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
            "tables": [
                {
                    "rowCount": 1, "columnCount": 1, "boundingRegions": [], "cells": [],
                    "spans": [], "caption": {"content": "T0", "boundingRegions": []},
                    "footnotes": [],
                },
            ],
            "sections": [
                {"elements": ["/paragraphs/0", "/paragraphs/1", "/tables/0", "/paragraphs/2"]},
            ],
        }
    )

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(StructureStage())
    reg.register(GraphStage())
    resolver = DagResolver(reg.all())

    assert "graph" in resolver.successors("ast")  # the chain is automatic
    assert "graph" in resolver.successors("structure")  # ... off BOTH deps

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
    await run_stage(stage_name="graph", run_id=str(uuid.uuid4()), services={}, **common)

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    graph_key = artifact_key(
        doc.id, "graph", 3,
        derived_input_hash([facts["ast"].output_hash, facts["structure"].output_hash]),
    )
    body = await store.get_json(graph_key)
    assert body["schema_version"] == 3
    assert body["nodes"] == [
        {"id": "doc", "type": "document", "label": "ACME LTD", "source_index": 0},
        {"id": "s0", "type": "section", "label": "Directors' Report", "source_index": 1},
        {"id": "s0p0", "type": "paragraph", "label": "The board is pleased.",
         "source_index": 2},
        {"id": "t0", "type": "table", "label": "T0", "source_index": None},
    ]
    assert body["edges"] == [
        {"source": "doc", "target": "s0", "type": "CONTAINS"},
        {"source": "s0", "target": "s0p0", "type": "CONTAINS"},
        {"source": "s0", "target": "t0", "type": "CONTAINS"},
    ]
