from fdi.core.retrieval import build_vector_results


def _hit(id_: str, score: float, metadata: dict[str, object] | None = None) -> dict[str, object]:
    return {"id": id_, "score": score, "metadata": metadata or {}}


def _rec(doc_id: str, chunk_id: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "doc_id": doc_id, "chunk_id": chunk_id, "heading": "H", "text": "T",
        "section_type": None, "page": None,
    }
    base.update(extra)
    return base


def test_no_filters_returns_all_resolvable_hits_in_order() -> None:
    hits = [_hit("d1:s0", 0.9), _hit("d1:s1", 0.5)]
    resolved = {"d1:s0": _rec("d1", "s0"), "d1:s1": _rec("d1", "s1")}
    docs: dict[str, dict[str, object]] = {}
    out = build_vector_results(
        hits, resolved, docs, section_type=None, doc_key=None, version=None, k=5
    )
    assert [r["chunk_id"] for r in out] == ["s0", "s1"]
    assert out[0]["doc_key"] is None and out[0]["version"] is None
    assert out[0]["score"] == 0.9
    assert out[0]["text"] == "T"


def test_section_type_filter_drops_non_matching() -> None:
    hits = [
        _hit("d1:s0", 0.9, {"section_type": "BALANCE_SHEET"}),
        _hit("d1:s1", 0.8, {"section_type": "NOTES"}),
        _hit("d1:s2", 0.7, {}),  # no metadata -> never matches a set filter
    ]
    resolved = {k: _rec("d1", k.split(":")[1]) for k in ("d1:s0", "d1:s1", "d1:s2")}
    out = build_vector_results(
        hits, resolved, {}, section_type="BALANCE_SHEET", doc_key=None, version=None, k=5
    )
    assert [r["chunk_id"] for r in out] == ["s0"]


def test_doc_key_and_version_filters_drop_mismatches_and_missing_docs() -> None:
    hits = [_hit("d1:s0", 0.9), _hit("d2:s0", 0.8), _hit("d3:s0", 0.7)]
    resolved = {k: _rec(k.split(":")[0], "s0") for k in ("d1:s0", "d2:s0", "d3:s0")}
    docs = {
        "d1": {"doc_key": "annual", "version": 1},
        "d2": {"doc_key": "annual", "version": 2},
        # d3 has no Document row at all
    }
    out = build_vector_results(
        hits, resolved, docs, section_type=None, doc_key="annual", version=1, k=5
    )
    assert [r["doc_id"] for r in out] == ["d1"]
    assert out[0]["doc_key"] == "annual" and out[0]["version"] == 1


def test_no_doc_key_or_version_filter_missing_doc_degrades_to_none_not_dropped() -> None:
    hits = [_hit("d1:s0", 0.9)]
    resolved = {"d1:s0": _rec("d1", "s0")}
    out = build_vector_results(
        hits, resolved, {}, section_type=None, doc_key=None, version=None, k=5
    )
    assert len(out) == 1
    assert out[0]["doc_key"] is None and out[0]["version"] is None


def test_unresolved_hit_is_dropped() -> None:
    hits = [_hit("d1:s0", 0.9), _hit("d1:s1", 0.8)]
    resolved = {"d1:s0": _rec("d1", "s0")}  # s1 never resolved
    out = build_vector_results(
        hits, resolved, {}, section_type=None, doc_key=None, version=None, k=5
    )
    assert [r["chunk_id"] for r in out] == ["s0"]


def test_truncates_to_k_without_resorting() -> None:
    # Deliberately NOT score-sorted going in, to prove no re-sort happens.
    hits = [_hit("d1:s0", 0.1), _hit("d1:s1", 0.9), _hit("d1:s2", 0.5)]
    resolved = {k: _rec("d1", k.split(":")[1]) for k in ("d1:s0", "d1:s1", "d1:s2")}
    out = build_vector_results(
        hits, resolved, {}, section_type=None, doc_key=None, version=None, k=2
    )
    assert [r["chunk_id"] for r in out] == ["s0", "s1"]  # input order, not score order


def test_empty_inputs() -> None:
    out = build_vector_results([], {}, {}, section_type=None, doc_key=None, version=None, k=5)
    assert out == []
