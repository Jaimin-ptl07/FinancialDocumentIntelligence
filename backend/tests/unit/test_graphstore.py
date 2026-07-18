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


def _load_two_section_doc(fake: FakeGraphStore, doc_id: str) -> None:
    nodes = [
        {"id": f"{doc_id}:doc", "type": "document", "label": "ACME", "doc_id": doc_id},
        {"id": f"{doc_id}:s0", "type": "section", "label": "First", "doc_id": doc_id},
        {"id": f"{doc_id}:s1", "type": "section", "label": "Second", "doc_id": doc_id},
        {"id": f"{doc_id}:s0p0", "type": "paragraph", "label": "P0", "doc_id": doc_id},
        {"id": f"{doc_id}:s1p0", "type": "paragraph", "label": "P1", "doc_id": doc_id},
    ]
    edges = [
        {"source": f"{doc_id}:doc", "target": f"{doc_id}:s0", "type": "CONTAINS"},
        {"source": f"{doc_id}:doc", "target": f"{doc_id}:s1", "type": "CONTAINS"},
        {"source": f"{doc_id}:s0", "target": f"{doc_id}:s1", "type": "READING_ORDER"},
        {"source": f"{doc_id}:s0", "target": f"{doc_id}:s0p0", "type": "CONTAINS"},
        {"source": f"{doc_id}:s1", "target": f"{doc_id}:s1p0", "type": "CONTAINS"},
    ]
    fake.load(nodes, edges)


def test_fake_neighbors_returns_both_directions() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    result = fake.neighbors("d1:s0")
    rels = {(r["node"]["id"], r["relationship"], r["direction"]) for r in result}
    assert ("d1:doc", "CONTAINS", "in") in rels
    assert ("d1:s1", "READING_ORDER", "out") in rels
    assert ("d1:s0p0", "CONTAINS", "out") in rels


def test_fake_neighbors_unknown_id_returns_empty() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    assert fake.neighbors("d1:ghost") == []


def test_fake_document_structure_orders_sections_and_paragraphs() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    out = fake.document_structure("d1")
    assert out["document"] == {"id": "d1:doc", "type": "document", "label": "ACME", "doc_id": "d1"}
    section_ids = [s["id"] for s in out["sections"]]
    assert section_ids == ["d1:s0", "d1:s1"]
    assert [p["id"] for p in out["sections"][0]["paragraphs"]] == ["d1:s0p0"]
    assert [p["id"] for p in out["sections"][1]["paragraphs"]] == ["d1:s1p0"]


def test_fake_document_structure_unknown_doc_id() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    assert fake.document_structure("no-such-doc") == {"document": None, "sections": []}


def test_fake_reading_order_matches_document_structure_sections_without_paragraphs() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    out = fake.reading_order("d1")
    assert [s["id"] for s in out] == ["d1:s0", "d1:s1"]
    assert all("paragraphs" not in s for s in out)


def test_fake_document_structure_scopes_by_doc_id_across_two_documents() -> None:
    fake = FakeGraphStore()
    _load_two_section_doc(fake, "d1")
    _load_two_section_doc(fake, "d2")
    out1 = fake.document_structure("d1")
    assert [s["id"] for s in out1["sections"]] == ["d1:s0", "d1:s1"]
