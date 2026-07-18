import pytest

from fdi.core.retrieval import fuse


def _hit(
    doc_id: str,
    chunk_id: str,
    *,
    doc_key: str | None = "K",
    version: int | None = 1,
    heading: str | None = "H",
    page: int | None = 1,
    section_type: str | None = "NOTES",
    text: str = "text",
) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "doc_key": doc_key,
        "version": version,
        "chunk_id": chunk_id,
        "heading": heading,
        "page": page,
        "section_type": section_type,
        "text": text,
        "score": 0.5,  # the input hit's own tool-specific score; fuse ignores it
    }


def test_fuse_empty_lists_returns_empty() -> None:
    assert fuse([], [], k=5) == []


def test_fuse_chunk_in_both_lists_outranks_single_list_chunk() -> None:
    a = _hit("d1", "s0")
    b = _hit("d1", "s1")
    out = fuse([a, b], [a], k=5)  # a: vector rank1 + keyword rank1; b: vector rank2 only
    ids = [f"{h['doc_id']}:{h['chunk_id']}" for h in out]
    assert ids == ["d1:s0", "d1:s1"]
    assert out[0]["score"] == pytest.approx(2 / 61)
    assert out[1]["score"] == pytest.approx(1 / 62)


def test_fuse_disjoint_lists_merge_by_score() -> None:
    a = _hit("d1", "s0")  # vector rank1
    b = _hit("d2", "s0")  # keyword rank1
    out = fuse([a], [b], k=5)
    ids = {f"{h['doc_id']}:{h['chunk_id']}" for h in out}
    assert ids == {"d1:s0", "d2:s0"}
    assert out[0]["score"] == pytest.approx(out[1]["score"])


def test_fuse_ties_broken_by_chunk_id_ascending() -> None:
    a = _hit("zzz", "s0")  # vector rank1, alone
    b = _hit("aaa", "s0")  # keyword rank1, alone -- same score as a
    out = fuse([a], [b], k=5)
    assert [f"{h['doc_id']}:{h['chunk_id']}" for h in out] == ["aaa:s0", "zzz:s0"]


def test_fuse_truncates_to_k_keeping_best_first() -> None:
    hits = [_hit(f"d{i}", "s0") for i in range(5)]
    out = fuse(hits, [], k=2)
    assert [f"{h['doc_id']}:{h['chunk_id']}" for h in out] == ["d0:s0", "d1:s0"]


def test_fuse_richest_citation_wins_on_dedup() -> None:
    thin = _hit("d1", "s0", doc_key=None, version=None, page=None, section_type=None)
    rich = _hit("d1", "s0", doc_key="K2", version=2, page=7, section_type="BALANCE_SHEET")
    out = fuse([thin], [rich], k=5)
    assert out[0]["doc_key"] == "K2"
    assert out[0]["version"] == 2
    assert out[0]["page"] == 7
    assert out[0]["section_type"] == "BALANCE_SHEET"


def test_fuse_richest_citation_tie_prefers_vector_hit_seen_first() -> None:
    v = _hit("d1", "s0", heading="from-vector")
    kw = _hit("d1", "s0", heading="from-keyword")
    out = fuse([v], [kw], k=5)  # equal richness (heading differs, both non-None) -> tie
    assert out[0]["heading"] == "from-vector"


def test_fuse_skips_hits_missing_doc_id_or_chunk_id() -> None:
    missing_doc_id: dict[str, object] = {"chunk_id": "s0"}
    missing_chunk_id: dict[str, object] = {"doc_id": "d1"}
    good = _hit("d1", "s1")
    out = fuse([missing_doc_id, missing_chunk_id, good], [], k=5)
    assert len(out) == 1
    assert out[0]["doc_id"] == "d1" and out[0]["chunk_id"] == "s1"


def test_fuse_custom_rrf_k_changes_score_value() -> None:
    a = _hit("d1", "s0")
    default_out = fuse([a], [], k=5)
    custom_out = fuse([a], [], k=5, rrf_k=10)
    assert default_out[0]["score"] == pytest.approx(1 / 61)
    assert custom_out[0]["score"] == pytest.approx(1 / 11)


def test_fuse_preserves_text_and_score_is_float() -> None:
    a = _hit("d1", "s0", text="the actual passage")
    out = fuse([a], [], k=5)
    assert out[0]["text"] == "the actual passage"
    assert isinstance(out[0]["score"], float)
