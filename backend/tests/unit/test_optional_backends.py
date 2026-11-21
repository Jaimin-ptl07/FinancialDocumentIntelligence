"""An optional backend that is not configured is a REPORTED outcome, not a crash.

Neo4j and Milvus are both opt-in: `workers/tasks.py` registers `graph_store` /
`vector_store` only when `from_settings` returns a client, and both return None
when the backend is unconfigured. Reading `ctx.services[...]` directly then
raised a bare KeyError -- which is how every `graph_load` run in this deployment
failed (2 of 2, `KeyError('graph_store')`), reading in the stage_run table as a
pipeline defect when the truth was simply that Neo4j was absent.
"""

import pytest

from fdi.pipeline.stage import StageContext
from fdi.pipeline.stages.graphload import GraphLoadStage
from fdi.pipeline.stages.index import IndexStage

_ARTIFACTS = {
    "graph": {"nodes": [{"id": "s0", "type": "section"}], "edges": []},
    "embedding": {"chunks": [{"chunk_id": "c0", "vector": [0.1, 0.2]}]},
    "chunks": {"chunks": [{"chunk_id": "c0", "section_type": "pl", "page": 1}]},
}


def _ctx(**services: object) -> StageContext:
    async def read(_doc_id: str, stage: str) -> object:
        return _ARTIFACTS[stage]

    return StageContext(doc_id="d1", read_artifact=read, services=services)


@pytest.mark.asyncio
async def test_graph_load_without_neo4j_reports_a_skip_instead_of_raising():
    receipt = await GraphLoadStage().run(_ctx())

    assert receipt == {
        "schema_version": 1, "loaded_nodes": 0, "loaded_edges": 0,
        "skipped": "graph_store not configured",
    }


@pytest.mark.asyncio
async def test_index_without_milvus_reports_a_skip_instead_of_raising():
    receipt = await IndexStage().run(_ctx())

    assert receipt == {
        "schema_version": 1, "upserted": 0,
        "skipped": "vector_store not configured",
    }


@pytest.mark.asyncio
async def test_a_configured_backend_is_still_used():
    """The guard must not swallow a store that IS present."""
    loaded: list[tuple[int, int]] = []

    class Store:
        def load(self, nodes, edges):
            loaded.append((len(nodes), len(edges)))
            return {"loaded_nodes": len(nodes), "loaded_edges": len(edges)}

    receipt = await GraphLoadStage().run(_ctx(graph_store=Store()))

    assert loaded == [(1, 0)]
    assert receipt["loaded_nodes"] == 1
    assert "skipped" not in receipt


def test_an_unreachable_milvus_degrades_instead_of_failing_the_api():
    """MEASURED: milvus aborted (exit 134) and the API then failed startup in a
    loop, taking down every route -- including the FOG screens, which use no
    vector store at all. `MilvusClient.__init__` opens a gRPC channel and raises
    when the server is not answering, and that call sits inside the lifespan.

    This codebase already holds that an unconfigured optional backend is a
    reportable outcome, not an exception. Unreachable is the same kind of fact."""
    from fdi.platform.vectorstore import MilvusVectorStore

    class Settings:
        milvus_uri = "http://127.0.0.1:1"  # nothing listens here
        milvus_collection = "c"
        embed_dim = 4

    assert MilvusVectorStore.from_settings(Settings()) is None  # type: ignore[arg-type]


def test_an_unconfigured_milvus_is_still_none():
    from fdi.platform.vectorstore import MilvusVectorStore

    class Settings:
        milvus_uri = ""
        milvus_collection = "c"
        embed_dim = 4

    assert MilvusVectorStore.from_settings(Settings()) is None  # type: ignore[arg-type]
