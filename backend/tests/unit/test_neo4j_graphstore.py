import pytest


def test_neo4j_store_loads_nodes_and_edges_via_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import graphstore as gs

    runs: list[tuple[str, dict[str, object]]] = []

    class _FakeTx:
        def run(self, query: str, **kwargs: object) -> None:
            runs.append((query, kwargs))

    class _FakeSession:
        def __enter__(self) -> "_FakeSession":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute_write(self, fn, *args: object) -> object:  # type: ignore[no-untyped-def]
            return fn(_FakeTx(), *args)

    class _FakeDriver:
        def session(self) -> _FakeSession:
            return _FakeSession()

    calls: dict[str, object] = {}

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: tuple[str, str]) -> _FakeDriver:
            calls["uri"] = uri
            calls["auth"] = auth
            return _FakeDriver()

    monkeypatch.setattr(gs, "GraphDatabase", _FakeGraphDatabase)
    store = gs.Neo4jGraphStore(uri="bolt://h:7687", user="neo4j", password="pw")
    assert calls["uri"] == "bolt://h:7687"
    assert calls["auth"] == ("neo4j", "pw")

    nodes = [{"id": "d1:doc", "type": "document", "label": "ACME", "doc_id": "d1"}]
    edges = [{"source": "d1:doc", "target": "d1:s0", "type": "CONTAINS"}]
    receipt = store.load(nodes, edges)
    assert receipt == {"loaded_nodes": 1, "loaded_edges": 1}

    node_runs = [r for r in runs if "MERGE (n:Document" in r[0]]
    edge_runs = [r for r in runs if "MERGE (a)-[r:CONTAINS]" in r[0]]
    assert len(node_runs) == 1
    assert node_runs[0][1] == {
        "id": "d1:doc", "type": "document", "label": "ACME", "doc_id": "d1",
    }
    assert len(edge_runs) == 1
    assert edge_runs[0][1] == {"source": "d1:doc", "target": "d1:s0", "type": "CONTAINS"}
    # the OLD generic label/rel-type must be gone
    assert not any("MERGE (n:Node" in r[0] for r in runs)
    assert not any(":REL" in r[0] for r in runs)


def test_neo4j_store_uses_section_and_paragraph_labels_and_reading_order_rel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fdi.platform import graphstore as gs

    runs: list[str] = []

    class _FakeTx:
        def run(self, query: str, **kwargs: object) -> None:
            runs.append(query)

    class _FakeSession:
        def __enter__(self) -> "_FakeSession":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute_write(self, fn, *args: object) -> object:  # type: ignore[no-untyped-def]
            return fn(_FakeTx(), *args)

    class _FakeDriver:
        def session(self) -> _FakeSession:
            return _FakeSession()

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: tuple[str, str]) -> _FakeDriver:
            return _FakeDriver()

    monkeypatch.setattr(gs, "GraphDatabase", _FakeGraphDatabase)
    store = gs.Neo4jGraphStore(uri="bolt://h:7687", user="neo4j", password="pw")

    nodes = [
        {"id": "d1:s0", "type": "section", "label": "S", "doc_id": "d1"},
        {"id": "d1:s0p0", "type": "paragraph", "label": "P", "doc_id": "d1"},
    ]
    edges = [{"source": "d1:s0", "target": "d1:s1", "type": "READING_ORDER"}]
    store.load(nodes, edges)

    assert any("MERGE (n:Section" in q for q in runs)
    assert any("MERGE (n:Paragraph" in q for q in runs)
    assert any("MERGE (a)-[r:READING_ORDER]" in q for q in runs)


def test_neo4j_store_uses_table_and_figure_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import graphstore as gs

    runs: list[str] = []

    class _FakeTx:
        def run(self, query: str, **kwargs: object) -> None:
            runs.append(query)

    class _FakeSession:
        def __enter__(self) -> "_FakeSession":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute_write(self, fn, *args: object) -> object:  # type: ignore[no-untyped-def]
            return fn(_FakeTx(), *args)

    class _FakeDriver:
        def session(self) -> _FakeSession:
            return _FakeSession()

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: tuple[str, str]) -> _FakeDriver:
            return _FakeDriver()

    monkeypatch.setattr(gs, "GraphDatabase", _FakeGraphDatabase)
    store = gs.Neo4jGraphStore(uri="bolt://h:7687", user="neo4j", password="pw")

    nodes = [
        {"id": "d1:t0", "type": "table", "label": "Table 0", "doc_id": "d1"},
        {"id": "d1:f0", "type": "figure", "label": "Figure 0", "doc_id": "d1"},
    ]
    edges = [{"source": "d1:s0", "target": "d1:t0", "type": "CONTAINS"}]
    store.load(nodes, edges)

    assert any("MERGE (n:Table" in q for q in runs)
    assert any("MERGE (n:Figure" in q for q in runs)


def test_neo4j_store_unknown_node_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import graphstore as gs

    class _FakeTx:
        def run(self, query: str, **kwargs: object) -> None:
            raise AssertionError("must not run any query for an unknown type")

    class _FakeSession:
        def __enter__(self) -> "_FakeSession":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def execute_write(self, fn, *args: object) -> object:  # type: ignore[no-untyped-def]
            return fn(_FakeTx(), *args)

    class _FakeDriver:
        def session(self) -> _FakeSession:
            return _FakeSession()

    class _FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: tuple[str, str]) -> _FakeDriver:
            return _FakeDriver()

    monkeypatch.setattr(gs, "GraphDatabase", _FakeGraphDatabase)
    store = gs.Neo4jGraphStore(uri="bolt://h:7687", user="neo4j", password="pw")

    with pytest.raises(ValueError, match="unknown graph node type"):
        store.load([{"id": "x", "type": "widget", "label": "?", "doc_id": "d1"}], [])


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.config import Settings
    from fdi.platform.graphstore import Neo4jGraphStore

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
    )
    assert Neo4jGraphStore.from_settings(s) is None
