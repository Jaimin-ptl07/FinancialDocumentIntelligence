import math

import pytest

from fdi.pipeline.vectorstore import FakeVectorStore, VectorStore


def test_fake_upsert_records_calls_and_returns_count() -> None:
    fake = FakeVectorStore()
    store: VectorStore = fake
    items = [{"id": "doc1:s0", "vector": [1.0, 0.0], "metadata": {"heading": "Balance Sheet"}}]
    receipt = store.upsert(items)
    assert receipt == {"upserted": 1}
    assert fake.calls == 1


def test_fake_search_ranks_by_cosine_similarity_best_first() -> None:
    fake = FakeVectorStore()
    fake.upsert(
        [
            {"id": "a", "vector": [1.0, 0.0], "metadata": {}},
            {"id": "b", "vector": [0.0, 1.0], "metadata": {}},
            {"id": "c", "vector": [0.9, 0.1], "metadata": {}},
        ]
    )
    results = fake.search([1.0, 0.0], k=2)
    assert [r["id"] for r in results] == ["a", "c"]
    assert results[0]["score"] == 1.0
    assert math.isclose(results[1]["score"], 0.9 / math.sqrt(0.9**2 + 0.1**2))


def test_fake_upsert_overwrites_by_id_not_duplicate() -> None:
    fake = FakeVectorStore()
    fake.upsert([{"id": "a", "vector": [1.0, 0.0], "metadata": {"heading": "old"}}])
    fake.upsert([{"id": "a", "vector": [0.0, 1.0], "metadata": {"heading": "new"}}])
    results = fake.search([0.0, 1.0], k=5)
    assert len(results) == 1
    assert results[0]["metadata"]["heading"] == "new"


def test_fake_search_empty_store_returns_empty_list() -> None:
    fake = FakeVectorStore()
    assert fake.search([1.0, 0.0], k=5) == []


def _item(item_id: str, vector: list[float], **metadata: object) -> dict[str, object]:
    return {"id": item_id, "vector": vector, "metadata": metadata}


def test_fake_search_text_scores_by_term_overlap() -> None:
    store = FakeVectorStore()
    store.upsert([
        _item("d:s1", [1.0, 0.0], text="revenue from operations total income"),
        _item("d:s2", [0.0, 1.0], text="revenue only"),
        _item("d:s3", [0.0, 1.0], text="unrelated narrative paragraph"),
    ])
    hits = store.search_text("revenue from operations", 10)
    ids = [h["id"] for h in hits]
    # 3-of-3 terms outranks 1-of-3; a zero-overlap chunk is not returned at all
    assert ids[0] == "d:s1"
    assert "d:s2" in ids
    assert "d:s3" not in ids


def test_fake_search_text_respects_k() -> None:
    store = FakeVectorStore()
    store.upsert([
        _item("d:s1", [1.0, 0.0], text="alpha beta"),
        _item("d:s2", [0.0, 1.0], text="alpha gamma"),
    ])
    assert len(store.search_text("alpha", 1)) == 1


def test_fake_search_text_empty_query_returns_nothing() -> None:
    store = FakeVectorStore()
    store.upsert([_item("d:s1", [1.0, 0.0], text="alpha")])
    assert store.search_text("   ", 10) == []


def test_fake_search_text_filter_scopes_by_doc_id() -> None:
    """The `in [...]` form is the ONLY filter production emits (see
    `_milvus_filter` in Task 5). The fake must parse exactly that."""
    store = FakeVectorStore()
    store.upsert([
        _item("a:s1", [1.0, 0.0], text="revenue", doc_id="a"),
        _item("b:s1", [0.0, 1.0], text="revenue", doc_id="b"),
    ])
    hits = store.search_text("revenue", 10, filter='doc_id in ["a"]')
    assert [h["id"] for h in hits] == ["a:s1"]


def test_fake_filter_accepts_multiple_doc_ids() -> None:
    store = FakeVectorStore()
    store.upsert([
        _item("a:s1", [1.0, 0.0], text="revenue", doc_id="a"),
        _item("b:s1", [0.0, 1.0], text="revenue", doc_id="b"),
        _item("c:s1", [0.0, 1.0], text="revenue", doc_id="c"),
    ])
    hits = store.search_text("revenue", 10, filter='doc_id in ["a", "c"]')
    assert sorted(h["id"] for h in hits) == ["a:s1", "c:s1"]


def test_fake_filter_rejects_a_shape_it_cannot_parse() -> None:
    """A fake that silently matched everything on an unrecognised filter would
    make a broken filter look like a passing test."""
    store = FakeVectorStore()
    store.upsert([_item("a:s1", [1.0, 0.0], text="revenue", doc_id="a")])
    with pytest.raises(ValueError):
        store.search_text("revenue", 10, filter="page > 5")


def test_fake_search_filter_scopes_vector_search_too() -> None:
    store = FakeVectorStore()
    store.upsert([
        _item("a:s1", [1.0, 0.0], doc_id="a"),
        _item("b:s1", [1.0, 0.0], doc_id="b"),
    ])
    hits = store.search([1.0, 0.0], 10, filter='doc_id in ["b"]')
    assert [h["id"] for h in hits] == ["b:s1"]


def test_fake_search_without_filter_is_unscoped() -> None:
    store = FakeVectorStore()
    store.upsert([
        _item("a:s1", [1.0, 0.0], doc_id="a"),
        _item("b:s1", [1.0, 0.0], doc_id="b"),
    ])
    assert len(store.search([1.0, 0.0], 10)) == 2
