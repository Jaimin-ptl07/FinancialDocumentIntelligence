"""EmbeddingStage auto-chains off ChunksStage (Phase 3a: `deps=("chunks",)`).
Phase 7 (`version=3`): it embeds ONLY non-row `chunks["chunks"]` records --
section (`s{i}`) and whole-table (`t{i}`) chunks -- and SKIPS fine-grained
table-row chunks (`t{i}r{j}`) via `fdi.core.retrieval.is_row_chunk_id`. Row
chunks stay in the `chunks` artifact untouched (keyword/FTS + resolve_chunks
unaffected); they simply leave the vector channel to de-dilute it. Everything
else about EmbeddingStage is unchanged: zero code change to IndexStage/
resolve_chunks (both are already generic over `chunk_id`)."""

import uuid
from collections.abc import Mapping
from typing import cast

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
from fdi.pipeline.stage import StageContext
from fdi.pipeline.stages.ast import AstStage
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


async def test_embedding_stage_skips_row_chunks_keeps_section_and_whole_table() -> None:
    """A `chunks` artifact with s0, t0, t0r0, t0r1 -> embedding.chunks has
    s0+t0 only (row chunks skipped); the embedder is called with 2 texts, not
    4; version == 3. Exercises EmbeddingStage.run directly against a StageContext
    reading a hand-built `chunks` artifact -- no DB/MinIO required."""
    chunks_artifact = {
        "schema_version": 1,
        "chunks": [
            {"chunk_id": "s0", "heading": "Balance Sheet", "text": "section text",
             "section_type": "BALANCE_SHEET", "page": 1, "para_count": 1},
            {"chunk_id": "t0", "heading": "Table 0", "text": "whole table text",
             "section_type": None, "page": 1, "para_count": None},
            {"chunk_id": "t0r0", "heading": "Table 0", "text": "row 0 text",
             "section_type": None, "page": 1, "para_count": None},
            {"chunk_id": "t0r1", "heading": "Table 0", "text": "row 1 text",
             "section_type": None, "page": 1, "para_count": None},
        ],
    }

    async def read_artifact(doc_id: str, stage_name: str) -> object:
        assert stage_name == "chunks"
        return chunks_artifact

    fake_embedder = FakeEmbeddingClient(dim=8)
    ctx = StageContext(
        doc_id="doc-1", read_artifact=read_artifact,
        services={"embedder": fake_embedder},
    )
    stage = EmbeddingStage()
    assert stage.version == 3

    result = cast(Mapping[str, object], await stage.run(ctx))

    assert fake_embedder.calls == 1
    assert fake_embedder.seen == ["section text", "whole table text"]
    result_chunks = cast(list[Mapping[str, object]], result["chunks"])
    assert [c["chunk_id"] for c in result_chunks] == ["s0", "t0"]


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
    await run_stage(stage_name="chunks", run_id=str(uuid.uuid4()), services={}, **common)
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()), services=embed_services, **common
    )
    # Second run: content-addressed cache hit -- must NOT call the fake embedder twice.
    await run_stage(
        stage_name="embedding", run_id=str(uuid.uuid4()), services=embed_services, **common
    )
    assert fake_embedder.calls == 1
    # embeds chunks["chunks"][].text VERBATIM, in order -- not re-derived from ast --
    # but SKIPS the row chunk (t0r1, Phase 7): only s0 + t0 are embedded.
    assert fake_embedder.seen == [
        "Balance Sheet\nAssets 100",
        "Balance Sheet\nValue\nCash: Value=100",
    ]

    async with session_factory() as s:
        facts = await RegistryRepository(s).runs_for_document(doc.id)

    embedding_key = artifact_key(
        doc.id, "embedding", 3, derived_input_hash([facts["chunks"].output_hash])
    )
    body = await store.get_json(embedding_key)
    assert body["model"] == "fake-embedding"
    assert body["dim"] == 8
    # s0 (section), t0 (whole-table) -- chunk_id/heading/order sourced from the
    # `chunks` artifact, byte-for-byte, not re-derived from `ast`; t0r1 (table
    # row) is skipped (Phase 7 de-dilution) though it remains in the `chunks`
    # artifact itself for keyword/FTS + resolve_chunks.
    assert [c["chunk_id"] for c in body["chunks"]] == ["s0", "t0"]
    assert body["chunks"][0]["heading"] == "Balance Sheet"
    assert body["chunks"][1]["heading"] == "Balance Sheet"
    assert len(body["chunks"][0]["vector"]) == 8
    assert len(body["chunks"][1]["vector"]) == 8
