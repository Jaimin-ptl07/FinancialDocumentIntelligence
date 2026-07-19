"""Neo4jGraphStore's three read methods, against a monkeypatched fake driver
that scripts Cypher-shaped row results per query -- no live Neo4j."""

import pytest


class _Row(dict):
    """A dict that also supports `row["key"]` AND `row.get("key")`, matching
    the neo4j Record interface closely enough for these unit tests."""


class _FakeResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(_Row(r) for r in self._rows)

    def single(self):
        return _Row(self._rows[0]) if self._rows else None


def _make_store(monkeypatch: pytest.MonkeyPatch, script: dict[str, list[dict[str, object]]]):
    """`script` maps a distinguishing substring of the Cypher text to the rows
    that query should return (matched by `in`, first match wins)."""
    from fdi.platform import graphstore as gs

    class _FakeTx:
        def run(self, query: str, **kwargs: object) -> _FakeResult:
            for needle, rows in script.items():
                if needle in query:
                    return _FakeResult(rows)
            raise AssertionError(f"unscripted query: {query}")

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute_read(self, fn, *args: object) -> object:  # type: ignore[no-untyped-def]
            return fn(_FakeTx(), *args)

    class _FakeDriver:
        def session(self) -> _FakeSession:
            return _FakeSession()

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: tuple[str, str]) -> _FakeDriver:
            return _FakeDriver()

    monkeypatch.setattr(gs, "GraphDatabase", _FakeGraphDatabase)
    return gs.Neo4jGraphStore(uri="bolt://h:7687", user="neo4j", password="pw")


def test_neighbors_builds_node_relationship_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store(
        monkeypatch,
        {
            "-[r]->": [
                {"relationship": "CONTAINS", "direction": "out", "id": "d1:s0p0",
                 "type": "paragraph", "label": "P", "doc_id": "d1"},
            ],
        },
    )
    # both directions come from the SAME UNION query in the real implementation;
    # the fake driver only distinguishes by substring, so script both halves
    # under keys that both appear in the one query text.
    result = store.neighbors("d1:s0")
    assert result[0]["node"]["id"] == "d1:s0p0"
    assert result[0]["relationship"] == "CONTAINS"
    assert result[0]["direction"] == "out"


def test_neighbors_unknown_id_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store(monkeypatch, {"-[r]->": []})
    assert store.neighbors("d1:ghost") == []


def test_document_structure_orders_sections_via_reading_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(
        monkeypatch,
        {
            "MATCH (d:Document": [{"id": "d1:doc", "label": "ACME"}],
            "READING_ORDER]->(b:Section": [{"source": "d1:s0", "target": "d1:s1"}],
            "MATCH (s:Section {doc_id: $doc_id}) RETURN": [
                {"id": "d1:s1", "label": "Second"},  # deliberately out of order
                {"id": "d1:s0", "label": "First"},
            ],
            "CONTAINS]->(p:Paragraph": [{"id": "d1:s0p0", "label": "P0", "section_id": "d1:s0"}],
            "READING_ORDER]->(b:Paragraph": [],
            "CONTAINS]->(n:Table": [],
            "CONTAINS]->(n:Figure": [],
        },
    )
    out = store.document_structure("d1")
    assert out["document"] == {"id": "d1:doc", "type": "document", "label": "ACME", "doc_id": "d1"}
    assert [s["id"] for s in out["sections"]] == ["d1:s0", "d1:s1"]
    assert [p["id"] for p in out["sections"][0]["paragraphs"]] == ["d1:s0p0"]
    assert out["sections"][1]["paragraphs"] == []
    assert out["sections"][0]["tables"] == []
    assert out["sections"][0]["figures"] == []


def test_document_structure_unknown_doc_returns_none_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(
        monkeypatch,
        {
            "MATCH (d:Document": [],
            "MATCH (s:Section {doc_id: $doc_id}) RETURN": [],
            "READING_ORDER]->(b:Section": [],
            "CONTAINS]->(p:Paragraph": [],
            "READING_ORDER]->(b:Paragraph": [],
            "CONTAINS]->(n:Table": [],
            "CONTAINS]->(n:Figure": [],
        },
    )
    out = store.document_structure("no-such-doc")
    assert out == {"document": None, "sections": []}


def test_document_structure_groups_and_numeric_sorts_tables_and_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(
        monkeypatch,
        {
            "MATCH (d:Document": [{"id": "d1:doc", "label": "ACME"}],
            "READING_ORDER]->(b:Section": [],
            "MATCH (s:Section {doc_id: $doc_id}) RETURN": [{"id": "d1:s0", "label": "First"}],
            "CONTAINS]->(p:Paragraph": [],
            "READING_ORDER]->(b:Paragraph": [],
            "CONTAINS]->(n:Table": [
                {"id": "d1:t10", "label": "Table 10", "section_id": "d1:s0"},
                {"id": "d1:t2", "label": "Table 2", "section_id": "d1:s0"},
            ],
            "CONTAINS]->(n:Figure": [
                {"id": "d1:f1", "label": "Figure 1", "section_id": "d1:s0"},
            ],
        },
    )
    out = store.document_structure("d1")
    section = out["sections"][0]
    assert [t["id"] for t in section["tables"]] == ["d1:t2", "d1:t10"]  # numeric, not lexicographic
    assert section["tables"][0]["type"] == "table"
    assert [f["id"] for f in section["figures"]] == ["d1:f1"]
    assert section["figures"][0]["type"] == "figure"


def test_reading_order_returns_sections_without_paragraphs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(
        monkeypatch,
        {
            "MATCH (s:Section {doc_id: $doc_id}) RETURN": [
                {"id": "d1:s0", "label": "First"},
            ],
            "READING_ORDER]->(b:Section": [],
        },
    )
    out = store.reading_order("d1")
    assert out == [{"id": "d1:s0", "type": "section", "label": "First", "doc_id": "d1"}]
