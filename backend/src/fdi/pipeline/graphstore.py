"""The GraphStore seam: a `load(nodes, edges) -> {loaded_nodes, loaded_edges}`
protocol. `Neo4jGraphStore` (platform, does I/O) structurally satisfies this
without importing it -- `platform` may not import `pipeline`. `FakeGraphStore`
lives here for tests: no live Neo4j call anywhere in the suite."""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class GraphStore(Protocol):
    def load(
        self, nodes: list[Mapping[str, object]], edges: list[Mapping[str, object]]
    ) -> dict[str, object]: ...


class FakeGraphStore:
    def __init__(self) -> None:
        self.calls = 0
        self.loaded_nodes: list[Mapping[str, object]] = []
        self.loaded_edges: list[Mapping[str, object]] = []

    def load(
        self, nodes: list[Mapping[str, object]], edges: list[Mapping[str, object]]
    ) -> dict[str, object]:
        self.calls += 1
        self.loaded_nodes.extend(nodes)
        self.loaded_edges.extend(edges)
        return {"loaded_nodes": len(nodes), "loaded_edges": len(edges)}
