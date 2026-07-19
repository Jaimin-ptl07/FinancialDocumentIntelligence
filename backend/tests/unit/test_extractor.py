from fdi.core.extractor import (
    Candidate,
    FieldSpec,
    assemble_result,
    build_extraction_messages,
    is_numeric,
    keyword_overlap,
    parse_field_specs,
    rerank_candidates,
)


def _schema() -> dict[str, object]:
    return {
        "modules": [
            {
                "id": 0, "name": "PREPROCESS", "folder": "0-preprocess",
                "fields": [
                    {  # metadata, not rag_extractable -- EXCLUDED
                        "field_name": "Company name", "source_type": "metadata",
                        "rag_extractable": False, "data_type": "string",
                        "retrieval_query": None, "extraction_prompt": "x", "keywords": [],
                    },
                    {  # rag_extractable + document_content -- KEPT
                        "field_name": "Total page count", "source_type": "document_content",
                        "rag_extractable": True, "data_type": "integer",
                        "retrieval_query": "Number of pages", "extraction_prompt": "Extract it.",
                        "keywords": ["pages", "total"],
                    },
                    {  # rag_extractable + document_content, but MALFORMED
                       # (retrieval_query null) -- EXCLUDED, never raises
                        "field_name": "Broken field", "source_type": "document_content",
                        "rag_extractable": True, "data_type": "string",
                        "retrieval_query": None, "extraction_prompt": "x", "keywords": [],
                    },
                ],
            },
            {
                "id": 1, "name": "BALANCE_SHEET", "folder": "1-balance-sheet",
                "fields": [
                    {
                        "field_name": "Total assets", "source_type": "document_content",
                        "rag_extractable": True, "data_type": "decimal",
                        "retrieval_query": "Total assets value",
                        "extraction_prompt": "Extract the total assets value.",
                        "keywords": ["total assets"], "enum_values": None,
                    },
                ],
            },
        ]
    }


def test_parse_field_specs_filters_rag_extractable_and_document_content() -> None:
    specs = parse_field_specs(_schema())
    names = [s.field_name for s in specs]
    assert names == ["Total page count", "Total assets"]


def test_parse_field_specs_skips_malformed_field() -> None:
    specs = parse_field_specs(_schema())
    assert "Broken field" not in [s.field_name for s in specs]


def test_parse_field_specs_module_filter_by_name() -> None:
    specs = parse_field_specs(_schema(), module="BALANCE_SHEET")
    assert [s.field_name for s in specs] == ["Total assets"]
    assert specs[0].module_id == 1 and specs[0].module_name == "BALANCE_SHEET"


def test_parse_field_specs_module_filter_by_folder() -> None:
    specs = parse_field_specs(_schema(), module="0-preprocess")
    assert [s.field_name for s in specs] == ["Total page count"]


def test_parse_field_specs_module_filter_no_match_is_empty() -> None:
    assert parse_field_specs(_schema(), module="no-such-module") == []


def test_parse_field_specs_empty_or_malformed_schema_never_raises() -> None:
    assert parse_field_specs({}) == []
    assert parse_field_specs({"modules": "not-a-list"}) == []
    assert parse_field_specs({"modules": [{"fields": "not-a-list"}]}) == []
    assert parse_field_specs({"modules": [{"fields": [1, "x", None]}]}) == []


def test_parse_field_specs_field_shape() -> None:
    spec = parse_field_specs(_schema())[0]
    assert spec == FieldSpec(
        field_name="Total page count", data_type="integer",
        retrieval_query="Number of pages", keywords=("pages", "total"),
        extraction_prompt="Extract it.", enum_values=None,
        module_id=0, module_name="PREPROCESS",
    )


def test_is_numeric() -> None:
    assert is_numeric("decimal") is True
    assert is_numeric("integer") is True
    for other in ("string", "date", "enum", "text", "link", "list", "boolean"):
        assert is_numeric(other) is False


def test_keyword_overlap_zero_partial_full_and_casefold() -> None:
    assert keyword_overlap("Total Assets 100", ["revenue"]) == 0.0
    assert keyword_overlap("Total Assets 100", ["total", "liabilities"]) == 0.5
    assert keyword_overlap("Total Assets 100", ["total", "assets"]) == 1.0
    assert keyword_overlap("TOTAL ASSETS", ["total"]) == 1.0  # casefold


def test_keyword_overlap_empty_keywords_is_zero() -> None:
    assert keyword_overlap("anything", []) == 0.0


def _cand(evidence_id: str, text: str, score: float) -> Candidate:
    return Candidate(evidence_id=evidence_id, text=text, page=1, score=score, source_tool="hybrid")


def test_rerank_candidates_keyword_boost_changes_order() -> None:
    low_score_relevant = _cand("a", "Total Assets 100", 0.4)
    high_score_irrelevant = _cand("b", "nothing relevant here", 0.5)
    ranked = rerank_candidates(
        [high_score_irrelevant, low_score_relevant], ["total", "assets"], keyword_weight=1.0
    )
    assert [c.evidence_id for c in ranked] == ["a", "b"]  # boost flips the order


def test_rerank_candidates_stable_on_ties() -> None:
    a, b, c = _cand("a", "x", 0.5), _cand("b", "x", 0.5), _cand("c", "x", 0.5)
    ranked = rerank_candidates([a, b, c], [])
    assert [x.evidence_id for x in ranked] == ["a", "b", "c"]  # original order preserved


def test_rerank_candidates_top_k_truncation() -> None:
    cands = [_cand(str(i), "x", float(i)) for i in range(5)]
    ranked = rerank_candidates(cands, [], top_k=2)
    assert [c.evidence_id for c in ranked] == ["4", "3"]


def test_rerank_candidates_empty_input() -> None:
    assert rerank_candidates([], ["k"]) == []


def test_rerank_candidates_max_per_source_none_is_byte_identical_to_prior_behavior() -> None:
    # Regression guard: max_per_source=None (the default) must reproduce every
    # existing rerank_candidates test exactly.
    low_score_relevant = _cand("a", "Total Assets 100", 0.4)
    high_score_irrelevant = _cand("b", "nothing relevant here", 0.5)
    ranked = rerank_candidates(
        [high_score_irrelevant, low_score_relevant], ["total", "assets"],
        keyword_weight=1.0, max_per_source=None,
    )
    assert [c.evidence_id for c in ranked] == ["a", "b"]

    a, b, c = _cand("a", "x", 0.5), _cand("b", "x", 0.5), _cand("c", "x", 0.5)
    ranked = rerank_candidates([a, b, c], [], max_per_source=None)
    assert [x.evidence_id for x in ranked] == ["a", "b", "c"]

    cands = [_cand(str(i), "x", float(i)) for i in range(5)]
    ranked = rerank_candidates(cands, [], top_k=2, max_per_source=None)
    assert [c.evidence_id for c in ranked] == ["4", "3"]

    assert rerank_candidates([], ["k"], max_per_source=None) == []


def test_rerank_candidates_max_per_source_caps_candidates_from_one_table() -> None:
    # 5 candidates share table t0 (evidence_id "table:doc:0"); 2 candidates are
    # from other sources. With max_per_source=2, at most 2 of the t0 candidates
    # survive even though top_k=6 would otherwise admit more of them (they all
    # blend-score higher than the other-source candidates).
    table_cands = [
        _cand("table:doc:0", f"t0 row {i}", 1.0 - i * 0.01) for i in range(5)
    ]
    other1 = _cand("doc:s0", "section text", 0.3)
    other2 = _cand("doc:t1", "other table text", 0.2)
    ranked = rerank_candidates(
        [*table_cands, other1, other2], [], top_k=6, max_per_source=2,
    )
    t0_count = sum(1 for c in ranked if c.evidence_id.startswith("table:doc:0"))
    assert t0_count == 2
    # remaining slots filled from other sources, not left empty
    assert len(ranked) == 4  # 2 from t0 + other1 + other2 (only 2 other-source candidates exist)
    assert {c.evidence_id for c in ranked} == {
        table_cands[0].evidence_id, table_cands[1].evidence_id,
        "doc:s0", "doc:t1",
    }


def test_rerank_candidates_max_per_source_source_key_variants() -> None:
    # "{doc}:t{i}r{j}" and "{doc}:t{i}" share the same source key "t{i}";
    # "{doc}:s{i}" keys on "s{i}"; anything else keys on the whole id.
    row_cands = [_cand(f"doc:t2r{i}", f"row {i}", 1.0 - i * 0.01) for i in range(4)]
    whole_table = _cand("doc:t2", "whole table text", 0.5)
    section = _cand("doc:s3", "section text", 0.4)
    lone = _cand("standalone-id", "lone text", 0.3)
    ranked = rerank_candidates(
        [*row_cands, whole_table, section, lone], [], top_k=8, max_per_source=2,
    )
    t2_count = sum(
        1 for c in ranked
        if c.evidence_id in {rc.evidence_id for rc in row_cands} or c.evidence_id == "doc:t2"
    )
    # row0 (0.99) and row1 (0.98) outscore whole_table (0.5) -- they win the
    # cap of 2 for shared source key "t2"; whole_table is squeezed out.
    assert t2_count == 2
    assert {c.evidence_id for c in ranked} == {
        "doc:t2r0", "doc:t2r1", "doc:s3", "standalone-id",
    }


def _field() -> FieldSpec:
    return FieldSpec(
        field_name="Total assets", data_type="decimal", retrieval_query="Total assets value",
        keywords=("total", "assets"), extraction_prompt="Extract the total assets value.",
        enum_values=None, module_id=1, module_name="BALANCE_SHEET",
    )


def test_build_extraction_messages_system_states_json_contract() -> None:
    system, _ = build_extraction_messages(_field(), [])
    assert "JSON" in system
    assert "only" in system.lower()
    assert "value" in system and "source" in system and "confidence" in system


def test_build_extraction_messages_system_states_extraction_guidance() -> None:
    """Hardened system prompt (field-agnostic): current-year column over
    prior-year comparative, primary statement over notes/schedules, label
    synonyms, and unit fidelity (no rescaling)."""
    system, _ = build_extraction_messages(_field(), [])
    system_cf = system.lower()
    assert "current reporting-year value" in system_cf
    assert "prior-year comparative" in system_cf
    assert "primary financial statement" in system_cf
    assert "label synonyms" in system_cf
    assert "do not" in system_cf and "rescale" in system_cf


def test_build_extraction_messages_user_carries_prompt_and_candidates() -> None:
    cands = [_cand("d1:s0", "Total assets were 100.", 0.9)]
    _, user = build_extraction_messages(_field(), cands)
    assert _field().extraction_prompt in user
    assert "d1:s0" in user
    assert "Total assets were 100." in user
    assert "1" in user  # the candidate's page


def test_assemble_result_full_llm_dict_passthrough() -> None:
    raw = {
        "value": 145, "confidence": 0.8,
        "source": {"page": 3, "snippet": "Total assets 145", "evidence_ids": ["d1:s0"]},
    }
    out = assemble_result(_field(), raw, [_cand("d1:s0", "Total assets 145", 0.9)])
    assert out == {
        "field_name": "Total assets", "value": 145,
        "source": {"page": 3, "snippet": "Total assets 145", "evidence_ids": ["d1:s0"]},
        "confidence": 0.8,
    }


def test_assemble_result_missing_keys_fall_back_to_candidates() -> None:
    ranked = [_cand("d1:s0", "Total assets were 100.", 0.9)]
    out = assemble_result(_field(), {}, ranked)
    assert out["value"] is None
    assert out["confidence"] == 0.0
    assert out["source"] == {
        "page": 1, "snippet": "Total assets were 100.", "evidence_ids": ["d1:s0"],
    }


def test_assemble_result_empty_pool_and_empty_raw_is_fully_none() -> None:
    out = assemble_result(_field(), {}, [])
    assert out["value"] is None
    assert out["confidence"] == 0.0
    assert out["source"] == {"page": None, "snippet": None, "evidence_ids": []}


def test_assemble_result_confidence_clamp() -> None:
    assert assemble_result(_field(), {"confidence": 1.7}, [])["confidence"] == 1.0
    assert assemble_result(_field(), {"confidence": -3}, [])["confidence"] == 0.0
    assert assemble_result(_field(), {"confidence": "x"}, [])["confidence"] == 0.0
    assert assemble_result(_field(), {}, [])["confidence"] == 0.0
    assert assemble_result(_field(), {"confidence": 0.42}, [])["confidence"] == 0.42


def test_assemble_result_malformed_source_falls_back() -> None:
    ranked = [_cand("d1:s0", "candidate text", 0.9)]
    out = assemble_result(_field(), {"source": "not-a-dict"}, ranked)
    assert out["source"] == {"page": 1, "snippet": "candidate text", "evidence_ids": ["d1:s0"]}


def test_assemble_result_malformed_evidence_ids_falls_back() -> None:
    ranked = [_cand("d1:s0", "candidate text", 0.9), _cand("d1:s1", "more text", 0.8)]
    out = assemble_result(_field(), {"source": {"evidence_ids": "not-a-list"}}, ranked)
    assert out["source"]["evidence_ids"] == ["d1:s0", "d1:s1"]
