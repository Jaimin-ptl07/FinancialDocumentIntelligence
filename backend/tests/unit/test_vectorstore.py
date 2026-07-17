import math

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
