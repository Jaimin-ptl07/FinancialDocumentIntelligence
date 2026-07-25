"""EmbeddingStage auto-chains off ChunksStage (`deps=("chunks",)`, `version=2`)
-- it embeds `chunks["chunks"][i]["text"]` in order, so the table CARD (TR-1
P2) is embedded "for free" alongside section chunks, with zero code change to
IndexStage/resolve_chunks (both are already generic over `chunk_id`)."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.semantic import SectionType
from fdi.pipeline.embeddings import FakeEmbeddingClient
from fdi.pipeline.intake import intake_document
from fdi.pipeline.keys import artifact_key, derived_input_hash
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
                {"role": "title", "content": "ACME LTD"},
                {"role": "sectionHeading", "content": "Balance Sheet"},
                {"content": "Assets 100"},
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


async def test_embedding_stage_runs_and_caches(  # fixtures from conftest
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

    fake_ocr = _fake_ocr()
    fake_semantic = FakeSemanticClient({"Balance Sheet": SectionType.BALANCE_SHEET.value})
    fake_embedder = FakeEmbeddingClient(dim=8)

    reg = StageRegistry()
    reg.register(OcrStage())
    reg.register(NormalizeStage())
    reg.register(AstStage())
    reg.register(SemanticStage())
    reg.register(GraphStage())
    reg.register(ProvenanceStage())
    reg.register(StructureStage())
    reg.register(BasisStage())
    reg.register(ChunksStage())
    reg.register(EmbeddingStage())
    resolver = DagResolver(reg.all())

    assert "embedding" in resolver.successors("chunks")  # the chain is automatic

    common = {
        "doc_id": doc.id,
        "correlation_id": "c1",
        "session_factory": session_factory,
        "store": store,
        "registry": reg,
        "resolver": resolver,
    }
    embed_services = {"embedder": fake_embedder}
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
    await run_stage(stage_name="basis", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()), services=embed_services, **common
    )
    # Second run: content-addressed cache hit -- must NOT call the fake embedder twice.
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()), services=embed_services, **common
    )
    assert fake_embedder.calls == 1
    # embeds chunks["chunks"][].text VERBATIM, in order -- not re-derived from ast.
    assert fake_embedder.seen == [
        "Balance Sheet\nAssets 100",
        "[Balance Sheet | BS | basis=unknown | page 1]\nColumns: Value\nRows: Cash",
    ]

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    embedding_key = artifact_key(
        doc.id, "embedding", 2, derived_input_hash([facts["chunks"].output_hash])
    )
    body = await store.get_json(embedding_key)
    assert body["model"] == "fake-embedding"
    assert body["dim"] == 8
    # s0 (section), t0 (ONE table card, TR-1 P2 -- no more t0r{j} row chunks)
    # -- chunk_id/heading/order sourced from the `chunks` artifact, byte-for-byte,
    # not re-derived from `ast`.
    assert [c["chunk_id"] for c in body["chunks"]] == ["s0", "t0"]
    assert body["chunks"][0]["heading"] == "Balance Sheet"
    assert body["chunks"][1]["heading"] == "Balance Sheet"
    assert len(body["chunks"][0]["vector"]) == 8
    assert len(body["chunks"][1]["vector"]) == 8
