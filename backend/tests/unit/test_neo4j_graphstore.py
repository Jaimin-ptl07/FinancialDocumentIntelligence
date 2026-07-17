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

    nodes = [{"id": "doc", "type": "document", "label": "ACME"}]
    edges = [{"source": "doc", "target": "s0", "type": "CONTAINS"}]
    receipt = store.load(nodes, edges)
    assert receipt == {"loaded_nodes": 1, "loaded_edges": 1}

    node_runs = [r for r in runs if "MERGE (n:Node" in r[0]]
    edge_runs = [r for r in runs if "MERGE (a)-[r:REL" in r[0]]
    assert len(node_runs) == 1
    assert node_runs[0][1] == {"id": "doc", "type": "document", "label": "ACME"}
    assert len(edge_runs) == 1
    assert edge_runs[0][1] == {"source": "doc", "target": "s0", "type": "CONTAINS"}


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.config import Settings
    from fdi.platform.graphstore import Neo4jGraphStore

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
    )
    assert Neo4jGraphStore.from_settings(s) is None
