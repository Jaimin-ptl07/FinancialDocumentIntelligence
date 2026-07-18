"""GraphLoadStage: a DERIVED stage that projects the SP-9 graph artifact
(nodes/edges) into a Neo4j-shaped GraphStore via an injected client.
Auto-chains off GraphStage via `deps=("graph",)`. The artifact is a small
deterministic receipt (node/edge counts) -- Neo4j is the side effect and the
actual point.

RT-5 (version 1 -> 2): every node id and every edge's source/target is
namespaced `{doc_id}:{bare_id}` before reaching `GraphStore.load` -- the fix
for the pre-RT-5 bug where every document's graph reused the identical bare
ids ("doc", "s0", "s0p1", ...) and a second document's load collided onto
the first's (MERGE last-writer-wins). Section nodes' namespaced id
(`{doc_id}:s{i}`) is therefore byte-identical to a Milvus hit id (SP-14) and
RT-1's `chunks[...].chunk_id` join key -- the unifying identity across
stores. Each node also gets a `doc_id` property (not just a namespaced id)
so `GraphStore.document_structure`/`reading_order` can scope a read by exact
property match rather than an unindexed id-prefix scan.

A cache hit (unchanged input_hash) does not re-run the load; a version bump
forces a real reload for every previously-loaded document (see spec §2
decision 12) -- `GraphStore.load` is idempotent MERGE either way."""

from collections.abc import Mapping
from typing import cast

from fdi.pipeline.graphstore import GraphStore
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


def _list_of_dicts(value: object) -> list[Mapping[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _namespace_node(doc_id: str, node: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": f"{doc_id}:{node.get('id')}",
        "type": node.get("type"),
        "label": node.get("label"),
        "doc_id": doc_id,
    }


def _namespace_edge(doc_id: str, edge: Mapping[str, object]) -> dict[str, object]:
    return {
        "source": f"{doc_id}:{edge.get('source')}",
        "target": f"{doc_id}:{edge.get('target')}",
        "type": edge.get("type"),
    }


class GraphLoadStage:
    name = "graph_load"
    version = 2
    deps: tuple[str, ...] = ("graph",)

    async def run(self, ctx: StageContext) -> object:
        graph = cast(Mapping[str, object], await ctx.read("graph"))
        store = cast(GraphStore, ctx.services["graph_store"])

        nodes: list[Mapping[str, object]] = [
            _namespace_node(ctx.doc_id, n) for n in _list_of_dicts(graph.get("nodes"))
        ]
        edges: list[Mapping[str, object]] = [
            _namespace_edge(ctx.doc_id, e) for e in _list_of_dicts(graph.get("edges"))
        ]

        receipt = store.load(nodes, edges)
        return {
            "schema_version": 1,
            "loaded_nodes": receipt["loaded_nodes"],
            "loaded_edges": receipt["loaded_edges"],
        }


REGISTRY.register(GraphLoadStage())
