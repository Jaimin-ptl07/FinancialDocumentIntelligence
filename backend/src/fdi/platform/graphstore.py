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

from fdi.core.graph import order_reading_chain
from fdi.platform.config import Settings
from neo4j import Driver, GraphDatabase, ManagedTransaction

_NODE_LABELS: Mapping[str, str] = {
    "document": "Document", "section": "Section", "paragraph": "Paragraph",
}
_EDGE_REL_TYPES: Mapping[str, str] = {"CONTAINS": "CONTAINS", "READING_ORDER": "READING_ORDER"}


def _cypher_label(node_type: object) -> str:
    """The real Cypher label for a graph node `type`, from a closed allowlist.

    Cypher does not allow a parameterized label ($type in a label position is
    a syntax error), so the label MUST be built into the query text -- doing
    that from an unvalidated `type` would let a malformed artifact inject
    arbitrary Cypher via the label position. Only this function's RETURN
    VALUE (one of the dict's fixed literals, never `node_type` itself) may
    ever reach an f-string. An unknown type raises rather than falling back
    to a generic label: it means `build_graph`'s type vocabulary changed
    without this module being updated, not a runtime fact to degrade past.
    """
    if not isinstance(node_type, str) or node_type not in _NODE_LABELS:
        raise ValueError(f"unknown graph node type: {node_type!r}; known: {sorted(_NODE_LABELS)}")
    return _NODE_LABELS[node_type]


def _cypher_rel_type(edge_type: object) -> str:
    """The real Cypher relationship type for a graph edge `type`. Same
    injection-safety and fail-loud rationale as `_cypher_label`."""
    if not isinstance(edge_type, str) or edge_type not in _EDGE_REL_TYPES:
        raise ValueError(
            f"unknown graph edge type: {edge_type!r}; known: {sorted(_EDGE_REL_TYPES)}"
        )
    return _EDGE_REL_TYPES[edge_type]


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
            label = _cypher_label(node.get("type"))
            tx.run(
                f"MERGE (n:{label} {{id: $id}}) "
                "SET n.type = $type, n.label = $label, n.doc_id = $doc_id",
                id=node["id"], type=node["type"], label=node["label"], doc_id=node.get("doc_id"),
            )
        for edge in edges:
            rel = _cypher_rel_type(edge.get("type"))
            tx.run(
                f"MATCH (a {{id: $source}}), (b {{id: $target}}) "
                f"MERGE (a)-[r:{rel}]->(b) SET r.type = $type",
                source=edge["source"], target=edge["target"], type=edge["type"],
            )
        return len(nodes), len(edges)

    def neighbors(self, node_id: str) -> list[dict[str, object]]:
        with self._driver.session() as session:
            return session.execute_read(self._neighbors_tx, node_id)

    @staticmethod
    def _neighbors_tx(tx: ManagedTransaction, node_id: str) -> list[dict[str, object]]:
        rows = tx.run(
            "MATCH (n {id: $id})-[r]->(m) "
            "RETURN type(r) AS relationship, 'out' AS direction, "
            "m.id AS id, m.type AS type, m.label AS label, m.doc_id AS doc_id "
            "UNION "
            "MATCH (n {id: $id})<-[r]-(m) "
            "RETURN type(r) AS relationship, 'in' AS direction, "
            "m.id AS id, m.type AS type, m.label AS label, m.doc_id AS doc_id",
            id=node_id,
        )
        return [
            {
                "node": {
                    "id": r["id"], "type": r["type"], "label": r["label"], "doc_id": r["doc_id"],
                },
                "relationship": r["relationship"],
                "direction": r["direction"],
            }
            for r in rows
        ]

    def document_structure(self, doc_id: str) -> dict[str, object]:
        with self._driver.session() as session:
            return session.execute_read(self._document_structure_tx, doc_id)

    @staticmethod
    def _sections_in_reading_order(tx: ManagedTransaction, doc_id: str) -> list[dict[str, object]]:
        sections = [
            {"id": r["id"], "type": "section", "label": r["label"], "doc_id": doc_id}
            for r in tx.run(
                "MATCH (s:Section {doc_id: $doc_id}) RETURN s.id AS id, s.label AS label",
                doc_id=doc_id,
            )
        ]
        edges = [
            {"source": r["source"], "target": r["target"], "type": "READING_ORDER"}
            for r in tx.run(
                "MATCH (a:Section {doc_id: $doc_id})-[:READING_ORDER]->"
                "(b:Section {doc_id: $doc_id}) RETURN a.id AS source, b.id AS target",
                doc_id=doc_id,
            )
        ]
        return order_reading_chain(sections, edges)

    @staticmethod
    def _document_structure_tx(tx: ManagedTransaction, doc_id: str) -> dict[str, object]:
        doc_row = tx.run(
            "MATCH (d:Document {doc_id: $doc_id}) RETURN d.id AS id, d.label AS label",
            doc_id=doc_id,
        ).single()
        document = (
            {"id": doc_row["id"], "type": "document", "label": doc_row["label"], "doc_id": doc_id}
            if doc_row is not None
            else None
        )
        ordered_sections = Neo4jGraphStore._sections_in_reading_order(tx, doc_id)

        paragraphs = [
            {"id": r["id"], "type": "paragraph", "label": r["label"], "section_id": r["section_id"]}
            for r in tx.run(
                "MATCH (s:Section {doc_id: $doc_id})-[:CONTAINS]->(p:Paragraph {doc_id: $doc_id}) "
                "RETURN p.id AS id, p.label AS label, s.id AS section_id",
                doc_id=doc_id,
            )
        ]
        para_edges = [
            {"source": r["source"], "target": r["target"], "type": "READING_ORDER"}
            for r in tx.run(
                "MATCH (a:Paragraph {doc_id: $doc_id})-[:READING_ORDER]->"
                "(b:Paragraph {doc_id: $doc_id}) RETURN a.id AS source, b.id AS target",
                doc_id=doc_id,
            )
        ]
        by_section: dict[str, list[dict[str, object]]] = {}
        for p in paragraphs:
            by_section.setdefault(str(p["section_id"]), []).append(
                {"id": p["id"], "type": p["type"], "label": p["label"], "doc_id": doc_id}
            )
        sections = [
            {**s, "paragraphs": order_reading_chain(by_section.get(str(s["id"]), []), para_edges)}
            for s in ordered_sections
        ]
        return {"document": document, "sections": sections}

    def reading_order(self, doc_id: str) -> list[dict[str, object]]:
        with self._driver.session() as session:
            return session.execute_read(self._sections_in_reading_order, doc_id)

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
