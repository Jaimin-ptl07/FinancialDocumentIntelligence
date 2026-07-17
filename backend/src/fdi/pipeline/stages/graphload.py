"""GraphLoadStage: a DERIVED stage that projects the SP-9 graph artifact
(nodes/edges) into a Neo4j-shaped GraphStore via an injected client.
Auto-chains off GraphStage via `deps=("graph",)`. The artifact is a small
deterministic receipt (node/edge counts) -- Neo4j is the side effect and the
actual point; see spec §2.5-6 for why a cache hit does not re-run the load
(the receipt satisfies SP-2's content-addressed cache, `GraphStore.load`
is idempotent MERGE, and a version bump forces a real reload)."""

from collections.abc import Mapping
from typing import cast

from fdi.pipeline.graphstore import GraphStore
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class GraphLoadStage:
    name = "graph_load"
    version = 1
    deps: tuple[str, ...] = ("graph",)

    async def run(self, ctx: StageContext) -> object:
        graph = cast(Mapping[str, object], await ctx.read("graph"))
        store = cast(GraphStore, ctx.services["graph_store"])

        nodes_raw = graph.get("nodes")
        edges_raw = graph.get("edges")
        nodes: list[Mapping[str, object]] = (
            [n for n in nodes_raw if isinstance(n, dict)] if isinstance(nodes_raw, list) else []
        )
        edges: list[Mapping[str, object]] = (
            [e for e in edges_raw if isinstance(e, dict)] if isinstance(edges_raw, list) else []
        )

        receipt = store.load(nodes, edges)
        return {
            "schema_version": 1,
            "loaded_nodes": receipt["loaded_nodes"],
            "loaded_edges": receipt["loaded_edges"],
        }


REGISTRY.register(GraphLoadStage())
