"""bm25_search + hybrid_search over a FakeVectorStore. No Postgres, no Milvus.

`_docs` stands in for the one DocumentRepository call the retrieval layer
makes; the tests monkeypatch it so the module's SQL never runs.
"""
import pytest

from fdi.pipeline import retrieval as r
from fdi.pipeline.vectorstore import FakeVectorStore


class _FakeEmbedder:
    model = "fake"

    def embed(self, texts: list[str]) -> list[list[float]]:
        # deterministic: "revenue" -> [1,0], anything else -> [0,1]
        return [[1.0, 0.0] if "revenue" in t.casefold() else [0.0, 1.0] for t in texts]


def _store() -> FakeVectorStore:
    s = FakeVectorStore()
    s.upsert([
        {"id": "d1:s0", "vector": [1.0, 0.0], "metadata": {
            "text": "revenue from operations total income", "doc_id": "d1",
            "chunk_id": "s0", "heading": "P&L", "page": 90, "section_type": None}},
        {"id": "d1:s1", "vector": [0.0, 1.0], "metadata": {
            "text": "unrelated narrative about governance", "doc_id": "d1",
            "chunk_id": "s1", "heading": "Report", "page": 30, "section_type": None}},
    ])
    return s


@pytest.fixture(autouse=True)
def _patch_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_docs(session: object, doc_ids: list[str]) -> dict[str, dict[str, object]]:
        return {d: {"doc_key": "acme", "version": 1} for d in doc_ids}

    monkeypatch.setattr(r, "_docs_for", _fake_docs)


async def test_bm25_search_returns_the_nine_key_hit_shape() -> None:
    hits = await r.bm25_search(
        session=None, vector_store=_store(), query="revenue from operations", k=5
    )
    assert hits
    assert set(hits[0]) == {
        "doc_id", "doc_key", "version", "chunk_id", "heading",
        "page", "section_type", "text", "score",
    }


async def test_bm25_search_ranks_by_term_overlap() -> None:
    hits = await r.bm25_search(
        session=None, vector_store=_store(), query="revenue from operations", k=5
    )
    # THE regression this whole sub-project exists for: a real query returns
    # something. The old plainto_tsquery path returned [] here.
    assert hits[0]["chunk_id"] == "s0"


async def test_bm25_search_empty_query_returns_empty() -> None:
    assert await r.bm25_search(
        session=None, vector_store=_store(), query="   ", k=5
    ) == []


async def test_bm25_search_doc_key_filter_matching_nothing_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A doc_key/version filter that resolves to zero doc_ids must return []
    -- NOT fall through to an unscoped whole-corpus search. `_milvus_filter([])`
    returns "" (unscoped), so if this guard were ever deleted, the seeded
    "revenue" chunk below would come back and this test would catch it."""

    async def _fake_doc_ids_for(
        session: object, *, doc_key: str | None, version: int | None
    ) -> list[str]:
        assert doc_key == "no-such-key"
        return []

    monkeypatch.setattr(r, "_doc_ids_for", _fake_doc_ids_for)

    hits = await r.bm25_search(
        session=None, vector_store=_store(), query="revenue from operations", k=5,
        filters={"doc_key": "no-such-key", "version": None},
    )
    assert hits == []


async def test_hybrid_search_fuses_both_channels() -> None:
    hits = await r.hybrid_search(
        session=None, embedder=_FakeEmbedder(), vector_store=_store(),
        query="revenue from operations", k=5,
    )
    # s0 wins both channels, so RRF must rank it first
    assert hits[0]["chunk_id"] == "s0"
    assert len(hits) >= 1


async def test_hybrid_search_a_both_channel_hit_outranks_a_single_channel_hit() -> None:
    """The property RRF exists for -- and the one the dead keyword channel has
    been silently failing to provide, since an empty keyword list makes every
    fused score come from the vector channel alone."""
    hits = await r.hybrid_search(
        session=None, embedder=_FakeEmbedder(), vector_store=_store(),
        query="revenue from operations", k=5,
    )
    by_chunk = {h["chunk_id"]: h["score"] for h in hits}
    assert by_chunk["s0"] > by_chunk.get("s1", 0.0)
