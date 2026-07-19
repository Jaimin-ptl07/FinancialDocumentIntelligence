"""The GraphStore seam: `load(nodes, edges) -> {loaded_nodes, loaded_edges}` plus
three read methods (`neighbors`, `document_structure`, `reading_order`) every
graph retrieval tool (RT-5) calls. `Neo4jGraphStore` (platform, does I/O)
structurally satisfies this without importing it -- `platform` may not import
`pipeline`. `FakeGraphStore` lives here for tests: no live Neo4j call anywhere
in the default suite. Node ids are namespaced `{doc_id}:{bare_id}` by
`GraphLoadStage` before ever reaching `load` -- see `fdi.core.graph.
order_reading_chain` for the shared READING_ORDER-walk both stores use to
answer `document_structure`/`reading_order` correctly (id string order is
NOT reading order -- "s10" < "s2" lexicographically)."""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from fdi.core.graph import numeric_id_sort_key, order_reading_chain


@runtime_checkable
class GraphStore(Protocol):
    def load(
        self, nodes: list[Mapping[str, object]], edges: list[Mapping[str, object]]
    ) -> dict[str, object]: ...

    def neighbors(self, node_id: str) -> list[dict[str, object]]: ...

    def document_structure(self, doc_id: str) -> dict[str, object]: ...

    def reading_order(self, doc_id: str) -> list[dict[str, object]]: ...


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

    def _node(self, node_id: object) -> dict[str, object] | None:
        return next((dict(n) for n in self.loaded_nodes if n.get("id") == node_id), None)

    def neighbors(self, node_id: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for e in self.loaded_edges:
            if e.get("source") == node_id:
                m = self._node(e.get("target"))
                if m is not None:
                    out.append({"node": m, "relationship": e.get("type"), "direction": "out"})
            elif e.get("target") == node_id:
                m = self._node(e.get("source"))
                if m is not None:
                    out.append({"node": m, "relationship": e.get("type"), "direction": "in"})
        return out

    def _members(
        self,
        scoped: list[Mapping[str, object]],
        contains: list[Mapping[str, object]],
        section_id: object,
        node_type: str,
        doc_id: str,
    ) -> list[dict[str, object]]:
        members = [
            dict(n)
            for n in scoped
            if n.get("type") == node_type
            and any(
                e.get("source") == section_id and e.get("target") == n.get("id")
                for e in contains
            )
        ]
        members.sort(key=lambda n: numeric_id_sort_key(doc_id, n.get("id")))
        return members

    def document_structure(self, doc_id: str) -> dict[str, object]:
        scoped = [n for n in self.loaded_nodes if n.get("doc_id") == doc_id]
        ids = {n.get("id") for n in scoped}
        scoped_edges = [
            e for e in self.loaded_edges if e.get("source") in ids and e.get("target") in ids
        ]
        document = next((dict(n) for n in scoped if n.get("type") == "document"), None)
        section_edges = [e for e in scoped_edges if e.get("type") == "READING_ORDER"]
        sections = order_reading_chain(
            [n for n in scoped if n.get("type") == "section"], section_edges
        )
        contains = [e for e in scoped_edges if e.get("type") == "CONTAINS"]
        para_ro = [e for e in scoped_edges if e.get("type") == "READING_ORDER"]
        section_views: list[dict[str, object]] = []
        for s in sections:
            paras = [
                dict(n)
                for n in scoped
                if n.get("type") == "paragraph"
                and any(
                    e.get("source") == s["id"] and e.get("target") == n.get("id")
                    for e in contains
                )
            ]
            section_views.append({
                **s,
                "paragraphs": order_reading_chain(paras, para_ro),
                "tables": self._members(scoped, contains, s["id"], "table", doc_id),
                "figures": self._members(scoped, contains, s["id"], "figure", doc_id),
            })
        return {"document": document, "sections": section_views}

    def reading_order(self, doc_id: str) -> list[dict[str, object]]:
        sections = self.document_structure(doc_id)["sections"]
        assert isinstance(sections, list)
        return [
            {k: v for k, v in s.items() if k not in {"paragraphs", "tables", "figures"}}
            for s in sections
        ]
