"""Opt-in smoke tests against a real Neo4j instance.

Skipped unless FDI_NEO4J_URI and FDI_NEO4J_PASSWORD are set. Never runs in CI
or in an unconfigured environment. The skipif marker is evaluated at
collection time and does not construct Settings or touch the network, so
collection is always safe."""

import os
import uuid

import pytest

from fdi.platform.config import get_settings
from fdi.platform.graphstore import Neo4jGraphStore

pytestmark = [
    pytest.mark.neo4j,
    pytest.mark.skipif(
        not (os.getenv("FDI_NEO4J_URI") and os.getenv("FDI_NEO4J_PASSWORD")),
        reason="Neo4j not configured",
    ),
]


def test_real_load_persists_nodes_and_edges_under_real_labels() -> None:
    store = Neo4jGraphStore.from_settings(get_settings())
    assert store is not None

    doc_id = f"sp9b-smoke-{uuid.uuid4()}"
    nodes = [
        {"id": f"{doc_id}:doc", "type": "document", "label": "ACME", "doc_id": doc_id},
        {"id": f"{doc_id}:s0", "type": "section", "label": "Directors' Report", "doc_id": doc_id},
    ]
    edges = [{"source": f"{doc_id}:doc", "target": f"{doc_id}:s0", "type": "CONTAINS"}]

    receipt = store.load(nodes, edges)
    assert receipt == {"loaded_nodes": 2, "loaded_edges": 1}

    with store._driver.session() as session:  # read-back proof, this test only
        count = session.run(
            "MATCH (n:Document {id: $id}) RETURN count(n) AS c", id=f"{doc_id}:doc"
        ).single()["c"]
    assert count == 1
    store.close()


def test_real_load_two_documents_do_not_collide() -> None:
    """The live-Neo4j proof of the RT-5 fix: two documents' graphs, loaded
    through the same store, must both survive independently."""
    store = Neo4jGraphStore.from_settings(get_settings())
    assert store is not None

    doc_a, doc_b = f"sp9b-a-{uuid.uuid4()}", f"sp9b-b-{uuid.uuid4()}"
    for doc_id, label in ((doc_a, "Company A"), (doc_b, "Company B")):
        store.load(
            [{"id": f"{doc_id}:doc", "type": "document", "label": label, "doc_id": doc_id}], []
        )

    with store._driver.session() as session:
        label_a = session.run(
            "MATCH (n:Document {id: $id}) RETURN n.label AS label", id=f"{doc_a}:doc"
        ).single()["label"]
        label_b = session.run(
            "MATCH (n:Document {id: $id}) RETURN n.label AS label", id=f"{doc_b}:doc"
        ).single()["label"]
    assert label_a == "Company A"
    assert label_b == "Company B"  # pre-fix, the second load would have overwritten the first
    store.close()


def test_real_read_methods_over_a_loaded_document() -> None:
    store = Neo4jGraphStore.from_settings(get_settings())
    assert store is not None

    doc_id = f"sp9b-reads-{uuid.uuid4()}"
    nodes = [
        {"id": f"{doc_id}:doc", "type": "document", "label": "ACME", "doc_id": doc_id},
        {"id": f"{doc_id}:s0", "type": "section", "label": "First", "doc_id": doc_id},
        {"id": f"{doc_id}:s1", "type": "section", "label": "Second", "doc_id": doc_id},
        {"id": f"{doc_id}:s0p0", "type": "paragraph", "label": "P0", "doc_id": doc_id},
    ]
    edges = [
        {"source": f"{doc_id}:doc", "target": f"{doc_id}:s0", "type": "CONTAINS"},
        {"source": f"{doc_id}:doc", "target": f"{doc_id}:s1", "type": "CONTAINS"},
        {"source": f"{doc_id}:s0", "target": f"{doc_id}:s1", "type": "READING_ORDER"},
        {"source": f"{doc_id}:s0", "target": f"{doc_id}:s0p0", "type": "CONTAINS"},
    ]
    store.load(nodes, edges)

    neighbors = store.neighbors(f"{doc_id}:s0")
    rels = {(n["node"]["id"], n["relationship"], n["direction"]) for n in neighbors}
    assert (f"{doc_id}:doc", "CONTAINS", "in") in rels
    assert (f"{doc_id}:s1", "READING_ORDER", "out") in rels

    structure = store.document_structure(doc_id)
    assert [s["id"] for s in structure["sections"]] == [f"{doc_id}:s0", f"{doc_id}:s1"]
    assert [p["id"] for p in structure["sections"][0]["paragraphs"]] == [f"{doc_id}:s0p0"]

    order = store.reading_order(doc_id)
    assert [s["id"] for s in order] == [f"{doc_id}:s0", f"{doc_id}:s1"]
    store.close()
