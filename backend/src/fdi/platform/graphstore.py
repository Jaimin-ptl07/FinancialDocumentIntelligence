"""Neo4jGraphStore -- GraphStore backed by a real Neo4j instance via the
official `neo4j` driver.

Lives in `platform` (does I/O), not `pipeline`: the `GraphStore` protocol and
`FakeGraphStore` live in `fdi.pipeline.graphstore`. This module structurally
satisfies that protocol without importing it -- `platform` may not import
`pipeline` per the layered architecture contract.

`GraphDatabase` is imported at module level (rather than inside `__init__`) so
tests can monkeypatch `fdi.platform.graphstore.GraphDatabase` with a fake --
no live Neo4j call in the suite."""

from collections.abc import Mapping

from neo4j import Driver, GraphDatabase, ManagedTransaction

from fdi.platform.config import Settings


class Neo4jGraphStore:
    """GraphStore backed by Neo4j. Lives in platform (does I/O)."""

    def __init__(self, *, uri: str, user: str, password: str) -> None:
        self._driver: Driver = GraphDatabase.driver(uri, auth=(user, password))

    def load(
        self, nodes: list[Mapping[str, object]], edges: list[Mapping[str, object]]
    ) -> dict[str, object]:
        with self._driver.session() as session:
            loaded_nodes, loaded_edges = session.execute_write(
                self._load_tx, nodes, edges
            )
        return {"loaded_nodes": loaded_nodes, "loaded_edges": loaded_edges}

    @staticmethod
    def _load_tx(
        tx: ManagedTransaction,
        nodes: list[Mapping[str, object]],
        edges: list[Mapping[str, object]],
    ) -> tuple[int, int]:
        for node in nodes:
            tx.run(
                "MERGE (n:Node {id: $id}) SET n.type = $type, n.label = $label",
                id=node["id"], type=node["type"], label=node["label"],
            )
        for edge in edges:
            tx.run(
                "MATCH (a:Node {id: $source}), (b:Node {id: $target}) "
                "MERGE (a)-[r:REL {type: $type}]->(b)",
                source=edge["source"], target=edge["target"], type=edge["type"],
            )
        return len(nodes), len(edges)

    def close(self) -> None:
        self._driver.close()

    @staticmethod
    def from_settings(settings: "Settings") -> "Neo4jGraphStore | None":
        if not settings.neo4j_uri or not settings.neo4j_password:
            return None
        return Neo4jGraphStore(
            uri=settings.neo4j_uri,
            user=settings.neo4j_user,
            password=settings.neo4j_password,
        )
