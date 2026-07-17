"""Opt-in smoke test against a real Milvus instance.

Skipped unless FDI_MILVUS_URI is set. Never runs in CI or in an unconfigured
environment. The skipif marker is evaluated at collection time and does not
construct Settings or touch the network, so collection is always safe."""

import os
import uuid

import pytest

from fdi.platform.config import get_settings
from fdi.platform.vectorstore import MilvusVectorStore

pytestmark = [
    pytest.mark.milvus,
    pytest.mark.skipif(
        not os.getenv("FDI_MILVUS_URI"),
        reason="Milvus not configured",
    ),
]


def test_real_upsert_and_search_finds_the_match() -> None:
    store = MilvusVectorStore.from_settings(get_settings())
    assert store is not None

    doc_id = f"sp14-smoke-{uuid.uuid4()}"
    dim = get_settings().embed_dim
    match_vector = [1.0] + [0.0] * (dim - 1)
    other_vector = [0.0] * (dim - 1) + [1.0]
    items = [
        {"id": f"{doc_id}:s0", "vector": match_vector, "metadata": {"heading": "Balance Sheet"}},
        {"id": f"{doc_id}:s1", "vector": other_vector, "metadata": {"heading": "Notes"}},
    ]
    receipt = store.upsert(items)
    assert receipt == {"upserted": 2}

    hits = store.search(match_vector, k=1)
    assert hits[0]["id"] == f"{doc_id}:s0"
