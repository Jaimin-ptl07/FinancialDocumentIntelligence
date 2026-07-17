from fdi.pipeline.graphstore import FakeGraphStore, GraphStore


def test_fake_records_calls_and_returns_counts() -> None:
    fake = FakeGraphStore()
    store: GraphStore = fake
    nodes = [{"id": "doc", "type": "document", "label": "ACME"}]
    edges = [{"source": "doc", "target": "s0", "type": "CONTAINS"}]
    receipt = store.load(nodes, edges)
    assert receipt == {"loaded_nodes": 1, "loaded_edges": 1}
    assert fake.calls == 1
    assert fake.loaded_nodes == nodes
    assert fake.loaded_edges == edges


def test_fake_accumulates_across_calls() -> None:
    fake = FakeGraphStore()
    fake.load([{"id": "a"}], [])
    fake.load([{"id": "b"}], [{"source": "a", "target": "b", "type": "X"}])
    assert fake.calls == 2
    assert len(fake.loaded_nodes) == 2
    assert len(fake.loaded_edges) == 1
