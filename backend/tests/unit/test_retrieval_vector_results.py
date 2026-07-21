from fdi.core.retrieval import build_vector_results


def _hit(full_id: str, score: float, **metadata: object) -> dict[str, object]:
    return {"id": full_id, "score": score, "metadata": metadata}


def test_build_vector_results_reads_text_off_the_hit() -> None:
    hits = [_hit("d1:s0", 0.9, text="Total assets 16,427.37",
                 heading="Balance Sheet", page=91, section_type="BALANCE_SHEET",
                 chunk_id="s0")]
    docs = {"d1": {"doc_key": "acme", "version": 2}}
    [rec] = build_vector_results(
        hits, docs, section_type=None, doc_key=None, version=None, k=10
    )
    assert rec == {
        "doc_id": "d1", "doc_key": "acme", "version": 2, "chunk_id": "s0",
        "heading": "Balance Sheet", "page": 91, "section_type": "BALANCE_SHEET",
        "text": "Total assets 16,427.37", "score": 0.9,
    }


def test_build_vector_results_filters_by_section_type() -> None:
    hits = [
        _hit("d1:s0", 0.9, text="a", section_type="BALANCE_SHEET", chunk_id="s0"),
        _hit("d1:s1", 0.8, text="b", section_type="NOTES", chunk_id="s1"),
    ]
    docs = {"d1": {"doc_key": "acme", "version": 2}}
    out = build_vector_results(
        hits, docs, section_type="NOTES", doc_key=None, version=None, k=10
    )
    assert [r["chunk_id"] for r in out] == ["s1"]


def test_build_vector_results_filters_by_doc_key_and_version() -> None:
    hits = [_hit("d1:s0", 0.9, text="a", chunk_id="s0"),
            _hit("d2:s0", 0.8, text="b", chunk_id="s0")]
    docs = {"d1": {"doc_key": "acme", "version": 2},
            "d2": {"doc_key": "other", "version": 1}}
    out = build_vector_results(
        hits, docs, section_type=None, doc_key="acme", version=2, k=10
    )
    assert [r["doc_id"] for r in out] == ["d1"]


def test_build_vector_results_truncates_without_resorting() -> None:
    hits = [_hit(f"d1:s{i}", 1.0 - i / 10, text="t", chunk_id=f"s{i}") for i in range(5)]
    docs = {"d1": {"doc_key": "acme", "version": 2}}
    out = build_vector_results(
        hits, docs, section_type=None, doc_key=None, version=None, k=2
    )
    assert [r["chunk_id"] for r in out] == ["s0", "s1"]


def test_build_vector_results_unknown_doc_degrades_citation_to_none() -> None:
    hits = [_hit("ghost:s0", 0.9, text="t", chunk_id="s0")]
    out = build_vector_results(
        hits, {}, section_type=None, doc_key=None, version=None, k=10
    )
    assert out[0]["doc_key"] is None and out[0]["version"] is None


def test_build_vector_results_malformed_metadata_never_raises() -> None:
    assert build_vector_results(
        [{"id": "d1:s0", "score": "x"}], {"d1": {"doc_key": "a", "version": 1}},
        section_type=None, doc_key=None, version=None, k=10,
    )[0]["score"] == 0.0
