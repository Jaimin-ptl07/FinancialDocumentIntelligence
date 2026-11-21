"""BM25 against a REAL Milvus. Carries the `milvus` marker: skipped unless
FDI_MILVUS_URI is set and reachable.

A fake cannot establish the claim this whole sub-project rests on -- that
BM25 ranking actually discriminates by term overlap -- so this is the one
test that must run against the real engine.
"""
import os
import uuid

import pytest
from pymilvus.exceptions import MilvusException

from fdi.platform.vectorstore import MilvusVectorStore

pytestmark = pytest.mark.milvus

# Captured at COLLECTION time, deliberately -- tests/conftest.py's autouse
# `_isolate_fdi_env` clears every FDI_* var from os.environ before each
# test's own fixtures run (so a developer's unrelated FDI_FOO does not leak
# into unrelated tests), which would erase FDI_MILVUS_URI before a
# function-scoped fixture body could read it via os.environ.get. Reading it
# once at import time, before any fixture executes, sidesteps that without
# touching conftest.py.
_MILVUS_URI = os.environ.get("FDI_MILVUS_URI")


@pytest.fixture
def store() -> MilvusVectorStore:
    if not _MILVUS_URI:
        pytest.skip("FDI_MILVUS_URI not set")
    # SET but UNREACHABLE is a third state, and it is the normal one on a
    # developer host: the configured URI is `milvus:19530`, the compose-network
    # name that api/worker resolve and the host cannot. Constructing the store
    # connects, so without this guard the fixture raises and pytest reports an
    # ERROR on a test that never ran -- even when `-m "not integration"`
    # deselects it. Skipping keeps "the backend is not available here" distinct
    # from "the code under test is broken".
    try:
        return MilvusVectorStore(
            uri=_MILVUS_URI, collection=f"test_bm25_{uuid.uuid4().hex[:8]}", dim=4
        )
    except MilvusException as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Milvus unreachable at {_MILVUS_URI}: {exc}")


def test_bm25_ranks_by_term_overlap_against_real_milvus(
    store: MilvusVectorStore,
) -> None:
    store.upsert([
        {"id": "d1:s0", "vector": [1.0, 0.0, 0.0, 0.0], "metadata": {
            "text": "revenue from operations and other income total",
            "doc_id": "d1", "chunk_id": "s0", "heading": None,
            "section_type": None, "page": 1}},
        {"id": "d1:s1", "vector": [0.0, 1.0, 0.0, 0.0], "metadata": {
            "text": "revenue only", "doc_id": "d1", "chunk_id": "s1",
            "heading": None, "section_type": None, "page": 2}},
        {"id": "d1:s2", "vector": [0.0, 0.0, 1.0, 0.0], "metadata": {
            "text": "entirely unrelated governance narrative",
            "doc_id": "d1", "chunk_id": "s2", "heading": None,
            "section_type": None, "page": 3}},
    ])
    hits = store.search_text("revenue from operations", 10)
    ids = [h["id"] for h in hits]
    assert ids[0] == "d1:s0", f"expected the 3-term match first, got {ids}"
    assert "d1:s2" not in ids, "a zero-overlap chunk must not be returned"


def test_bm25_filter_scopes_to_one_document(store: MilvusVectorStore) -> None:
    store.upsert([
        {"id": "a:s0", "vector": [1.0, 0.0, 0.0, 0.0], "metadata": {
            "text": "revenue from operations", "doc_id": "a", "chunk_id": "s0",
            "heading": None, "section_type": None, "page": 1}},
        {"id": "b:s0", "vector": [0.0, 1.0, 0.0, 0.0], "metadata": {
            "text": "revenue from operations", "doc_id": "b", "chunk_id": "s0",
            "heading": None, "section_type": None, "page": 1}},
    ])
    hits = store.search_text("revenue from operations", 10, filter='doc_id in ["a"]')
    assert [h["id"] for h in hits] == ["a:s0"]
