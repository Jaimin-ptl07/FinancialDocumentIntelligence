"""Opt-in smoke test against a real Neo4j instance.

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


def test_real_load_persists_nodes_and_edges() -> None:
    store = Neo4jGraphStore.from_settings(get_settings())
    assert store is not None

    doc_id = f"sp9b-smoke-{uuid.uuid4()}"
    nodes = [
        {"id": f"{doc_id}-doc", "type": "document", "label": "ACME"},
        {"id": f"{doc_id}-s0", "type": "section", "label": "Directors' Report"},
    ]
    edges = [{"source": f"{doc_id}-doc", "target": f"{doc_id}-s0", "type": "CONTAINS"}]

    receipt = store.load(nodes, edges)
    assert receipt == {"loaded_nodes": 2, "loaded_edges": 1}

    with store._driver.session() as session:  # read-back proof, this test only
        count = session.run(
            "MATCH (n:Node {id: $id}) RETURN count(n) AS c", id=f"{doc_id}-doc"
        ).single()["c"]
    assert count == 1
    store.close()
