from fdi.core.extractor import (
    Candidate,
    FieldSpec,
    UnitSpec,
    assemble_result,
    build_extraction_messages,
    filter_candidates,
    find_consolidated_start_page,
    group_fields_by_unit,
    is_numeric,
    keyword_overlap,
    parse_field_specs,
    rerank_candidates,
    unit_needs_tables,
    unit_top_k,
)


def _schema() -> dict[str, object]:
    """A v3.1.0-shaped schema: modules -> retrieval_units -> fields, with
    `aliases` per field and `locator_keywords`/`retrieval_query` per unit.
    Mirrors docs/architecture/field-extraction-schema.json exactly (no
    rag_extractable, no source_type, no per-field retrieval_query)."""
    return {
        "version": "3.1.0",
        "modules": [
            {
                "id": 2,
                "key": "module_2_company_info",
                "name": "Company Information",
                "retrieval_units": [
                    {
                        "unit_id": "RU_COMPANY",
                        "title": "Corporate Information",
                        "scope": "front_matter",
                        "locator_keywords": ["corporate information", "CIN"],
                        "retrieval_query": "Corporate Information panel: name, CIN, address",
                        "fields": [
                            {
                                "field_name": "Company name",
                                "canonical_label": "Company name",
                                "aliases": ["Company name", "Name of the company"],
                                "data_type": "string",
                                "extraction_prompt": "Extract the Company name.",
                            },
                            {
                                # MALFORMED: no extraction_prompt -- SKIPPED, never raises
                                "field_name": "Broken field",
                                "aliases": [],
                                "data_type": "string",
                            },
                        ],
                    },
                ],
            },
            {
                "id": 3,
                "key": "module_3_balance_sheet",
                "name": "Balance Sheet",
                "retrieval_units": [
                    {
                        "unit_id": "RU_BS",
                        "title": "Balance Sheet",
                        "scope": "primary_statement",
                        "statement_type": "BS",
                        "locator_keywords": ["balance sheet", "total assets"],
                        "retrieval_query": "Standalone Balance Sheet as at the reporting date",
                        "fields": [
                            {
                                "field_name": "Total assets",
                                "canonical_label": "Total assets",
                                "aliases": ["Total assets", "TOTAL ASSETS"],
                                "data_type": "decimal",
                                "extraction_prompt": "Extract the total assets value.",
                            },
                        ],
                    },
                ],
            },
        ],
    }


def test_parse_field_specs_flattens_modules_units_fields() -> None:
    specs = parse_field_specs(_schema())
    assert [s.field_name for s in specs] == ["Company name", "Total assets"]


def test_parse_field_specs_skips_malformed_field() -> None:
    specs = parse_field_specs(_schema())
    assert "Broken field" not in [s.field_name for s in specs]


def test_parse_field_specs_module_filter_by_key() -> None:
    specs = parse_field_specs(_schema(), module="module_3_balance_sheet")
    assert [s.field_name for s in specs] == ["Total assets"]
    assert specs[0].module_id == 3
    assert specs[0].module_key == "module_3_balance_sheet"
    assert specs[0].module_name == "Balance Sheet"


def test_parse_field_specs_module_filter_by_name() -> None:
    specs = parse_field_specs(_schema(), module="Company Information")
    assert [s.field_name for s in specs] == ["Company name"]


def test_parse_field_specs_module_filter_no_match_is_empty() -> None:
    assert parse_field_specs(_schema(), module="no-such-module") == []


def test_parse_field_specs_empty_or_malformed_schema_never_raises() -> None:
    assert parse_field_specs({}) == []
    assert parse_field_specs({"modules": "not-a-list"}) == []
    assert parse_field_specs({"modules": [{"retrieval_units": "nope"}]}) == []
    assert parse_field_specs({"modules": [{"retrieval_units": [1, "x", None]}]}) == []
    assert parse_field_specs({"modules": [{"retrieval_units": [{"fields": "nope"}]}]}) == []
    assert parse_field_specs({"modules": [{"retrieval_units": [{"fields": [1, None]}]}]}) == []


def test_parse_field_specs_retrieval_query_comes_from_the_unit() -> None:
    spec = parse_field_specs(_schema(), module="module_3_balance_sheet")[0]
    assert spec.retrieval_query == "Standalone Balance Sheet as at the reporting date"
    assert spec.unit_id == "RU_BS"
    assert spec.unit_title == "Balance Sheet"
    assert spec.scope == "primary_statement"
    assert spec.statement_type == "BS"


def test_parse_field_specs_per_field_retrieval_query_is_the_fallback() -> None:
    """A unit with no usable retrieval_query falls back to the field's own,
    so a future schema revision that pushes the query back down still parses."""
    schema = {
        "modules": [
            {
                "id": 9, "key": "m9", "name": "M9",
                "retrieval_units": [
                    {
                        "unit_id": "RU_9", "title": "Nine", "retrieval_query": "   ",
                        "locator_keywords": ["nine"],
                        "fields": [
                            {
                                "field_name": "F", "data_type": "string",
                                "extraction_prompt": "p", "aliases": ["f"],
                                "retrieval_query": "field level query",
                            },
                        ],
                    },
                ],
            },
        ],
    }
    assert parse_field_specs(schema)[0].retrieval_query == "field level query"


def test_parse_field_specs_field_with_no_query_anywhere_is_skipped() -> None:
    schema = {
        "modules": [
            {
                "id": 9, "key": "m9", "name": "M9",
                "retrieval_units": [
                    {
                        "unit_id": "RU_9", "title": "Nine",
                        "fields": [
                            {
                                "field_name": "F", "data_type": "string",
                                "extraction_prompt": "p", "aliases": [],
                            },
                        ],
                    },
                ],
            },
        ],
    }
    assert parse_field_specs(schema) == []


def test_parse_field_specs_keywords_are_aliases_then_locator_keywords() -> None:
    spec = parse_field_specs(_schema(), module="module_3_balance_sheet")[0]
    assert spec.keywords == ("Total assets", "TOTAL ASSETS", "balance sheet", "total assets")


def test_parse_field_specs_keywords_are_deduplicated_in_order() -> None:
    """keyword_overlap divides by len(keywords), so a duplicate silently
    dilutes the rerank boost -- dedup is a scoring correctness rule, not tidying."""
    schema = {
        "modules": [
            {
                "id": 9, "key": "m9", "name": "M9",
                "retrieval_units": [
                    {
                        "unit_id": "RU_9", "title": "Nine", "retrieval_query": "q",
                        "locator_keywords": ["total assets", "balance sheet"],
                        "fields": [
                            {
                                "field_name": "F", "data_type": "string",
                                "extraction_prompt": "p",
                                "aliases": ["total assets", "assets"],
                            },
                        ],
                    },
                ],
            },
        ],
    }
    assert parse_field_specs(schema)[0].keywords == (
        "total assets", "assets", "balance sheet",
    )


def test_parse_field_specs_field_shape() -> None:
    spec = parse_field_specs(_schema())[0]
    assert spec == FieldSpec(
        field_name="Company name",
        data_type="string",
        retrieval_query="Corporate Information panel: name, CIN, address",
        keywords=("Company name", "Name of the company", "corporate information", "CIN"),
        extraction_prompt="Extract the Company name.",
        enum_values=None,
        module_id=2,
        module_name="Company Information",
        module_key="module_2_company_info",
        unit_id="RU_COMPANY",
        unit_title="Corporate Information",
        scope="front_matter",
        statement_type=None,
        field_path="",
        periods=(),
        locator_keywords=("corporate information", "CIN"),
    )


def test_parse_field_specs_against_the_committed_schema_file() -> None:
    """The regression that motivated this task: the shipped v2.0 parser
    returned [] against the committed v3.1.0 file, so POST /extract/document
    extracted nothing at all. Reads the real file, not a fixture."""
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    path = repo_root / "docs" / "architecture" / "field-extraction-schema.json"
    with open(path, encoding="utf-8") as f:
        schema = json.load(f)
    specs = parse_field_specs(schema)
    assert len(specs) == 198
    assert parse_field_specs(schema, module="module_3_balance_sheet")
    assert all(s.retrieval_query.strip() for s in specs)
    assert all(s.unit_id for s in specs)


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


def _scored_cand(evidence_id: str, text: str, score: float) -> Candidate:
    """Fixed page=1, variable score -- for rerank/assemble tests that vary
    score to test ordering/fallback behavior. Distinct from the Task 3
    `_cand` helper below, which fixes score and varies page instead."""
    return Candidate(evidence_id=evidence_id, text=text, page=1, score=score, source_tool="hybrid")


def _fs(name: str, *, unit_id: str = "U1", data_type: str = "decimal",
        query: str = "q", locators: tuple[str, ...] = ("kw",)) -> FieldSpec:
    return FieldSpec(
        field_name=name, data_type=data_type, retrieval_query=query,
        keywords=(name,), extraction_prompt=f"Extract {name}.", enum_values=None,
        module_id=1, module_name="M", unit_id=unit_id, unit_title=f"T-{unit_id}",
        field_path=f"m.{name}", locator_keywords=locators,
    )


def test_rerank_candidates_keyword_boost_changes_order() -> None:
    low_score_relevant = _scored_cand("a", "Total Assets 100", 0.4)
    high_score_irrelevant = _scored_cand("b", "nothing relevant here", 0.5)
    ranked = rerank_candidates(
        [high_score_irrelevant, low_score_relevant], ["total", "assets"], keyword_weight=1.0
    )
    assert [c.evidence_id for c in ranked] == ["a", "b"]  # boost flips the order


def test_rerank_candidates_stable_on_ties() -> None:
    a, b, c = _scored_cand("a", "x", 0.5), _scored_cand("b", "x", 0.5), _scored_cand("c", "x", 0.5)
    ranked = rerank_candidates([a, b, c], [])
    assert [x.evidence_id for x in ranked] == ["a", "b", "c"]  # original order preserved


def test_rerank_candidates_top_k_truncation() -> None:
    cands = [_scored_cand(str(i), "x", float(i)) for i in range(5)]
    ranked = rerank_candidates(cands, [], top_k=2)
    assert [c.evidence_id for c in ranked] == ["4", "3"]


def test_rerank_candidates_empty_input() -> None:
    assert rerank_candidates([], ["k"]) == []


def _field() -> FieldSpec:
    return FieldSpec(
        field_name="Total assets", data_type="decimal",
        retrieval_query="Standalone Balance Sheet as at the reporting date",
        keywords=("total", "assets"), extraction_prompt="Extract the total assets value.",
        enum_values=None, module_id=3, module_name="Balance Sheet",
        module_key="module_3_balance_sheet", unit_id="RU_BS", unit_title="Balance Sheet",
        scope="primary_statement", statement_type="BS",
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
    cands = [_scored_cand("d1:s0", "Total assets were 100.", 0.9)]
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
    out = assemble_result(_field(), raw, [_scored_cand("d1:s0", "Total assets 145", 0.9)])
    assert out == {
        "field_name": "Total assets", "value": 145,
        "source": {"page": 3, "snippet": "Total assets 145", "evidence_ids": ["d1:s0"]},
        "confidence": 0.8,
    }


def test_assemble_result_missing_keys_fall_back_to_candidates() -> None:
    ranked = [_scored_cand("d1:s0", "Total assets were 100.", 0.9)]
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
    ranked = [_scored_cand("d1:s0", "candidate text", 0.9)]
    out = assemble_result(_field(), {"source": "not-a-dict"}, ranked)
    assert out["source"] == {"page": 1, "snippet": "candidate text", "evidence_ids": ["d1:s0"]}


def test_assemble_result_malformed_evidence_ids_falls_back() -> None:
    ranked = [_scored_cand("d1:s0", "candidate text", 0.9), _scored_cand("d1:s1", "more text", 0.8)]
    out = assemble_result(_field(), {"source": {"evidence_ids": "not-a-list"}}, ranked)
    assert out["source"]["evidence_ids"] == ["d1:s0", "d1:s1"]


def test_parse_field_specs_carries_field_path_periods_and_locator_keywords() -> None:
    schema = {
        "modules": [
            {
                "id": 3,
                "key": "module_3_balance_sheet",
                "name": "Balance Sheet",
                "retrieval_units": [
                    {
                        "unit_id": "RU_BS",
                        "title": "Balance Sheet",
                        "locator_keywords": ["balance sheet", "total assets"],
                        "retrieval_query": "Balance Sheet",
                        "fields": [
                            {
                                "field_name": "Total equity",
                                "field_path": "module_3_balance_sheet.equity.total_equity",
                                "aliases": ["Total equity"],
                                "data_type": "decimal",
                                "periods": ["current", "comparative_prior"],
                                "extraction_prompt": "Extract total equity.",
                            },
                        ],
                    },
                ],
            },
        ],
    }
    [spec] = parse_field_specs(schema)
    assert spec.field_path == "module_3_balance_sheet.equity.total_equity"
    assert spec.periods == ("current", "comparative_prior")
    assert spec.locator_keywords == ("balance sheet", "total assets")
    # keywords keeps its existing aliases-then-locator_keywords contract
    assert spec.keywords == ("Total equity", "balance sheet", "total assets")


def test_parse_field_specs_missing_field_path_and_periods_default_empty() -> None:
    schema = {
        "modules": [
            {
                "id": 2,
                "key": "m2",
                "name": "M2",
                "retrieval_units": [
                    {
                        "unit_id": "RU_X",
                        "title": "X",
                        "locator_keywords": [],
                        "retrieval_query": "x",
                        "fields": [
                            {
                                "field_name": "No path",
                                "aliases": [],
                                "data_type": "string",
                                "extraction_prompt": "Extract it.",
                            },
                        ],
                    },
                ],
            },
        ],
    }
    [spec] = parse_field_specs(schema)
    assert spec.field_path == ""
    assert spec.periods == ()
    assert spec.locator_keywords == ()


def test_field_spec_constructs_without_the_new_members() -> None:
    """Hand-built FieldSpecs (tests, synthesizing callers) must keep working."""
    spec = FieldSpec(
        field_name="F", data_type="string", retrieval_query="q",
        keywords=("k",), extraction_prompt="p", enum_values=None,
        module_id=1, module_name="M",
    )
    assert spec.field_path == ""
    assert spec.periods == ()
    assert spec.locator_keywords == ()


def test_group_fields_by_unit_groups_and_preserves_order() -> None:
    fields = [_fs("a", unit_id="U1"), _fs("b", unit_id="U2"), _fs("c", unit_id="U1")]
    units: list[UnitSpec] = group_fields_by_unit(fields)
    assert [u.unit_id for u in units] == ["U1", "U2"]
    assert [f.field_name for f in units[0].fields] == ["a", "c"]
    assert [f.field_name for f in units[1].fields] == ["b"]


def test_group_fields_by_unit_carries_unit_metadata() -> None:
    fields = [_fs("a", unit_id="RU_BS", query="Balance Sheet", locators=("balance sheet",))]
    [unit] = group_fields_by_unit(fields)
    assert unit.unit_id == "RU_BS"
    assert unit.unit_title == "T-RU_BS"
    assert unit.retrieval_query == "Balance Sheet"
    assert unit.locator_keywords == ("balance sheet",)


def test_group_fields_by_unit_blank_unit_id_makes_singleton_units() -> None:
    """A hand-built FieldSpec has unit_id="" -- those must NOT all collapse
    into one bogus group."""
    fields = [_fs("a", unit_id=""), _fs("b", unit_id="")]
    units = group_fields_by_unit(fields)
    assert len(units) == 2
    assert [len(u.fields) for u in units] == [1, 1]


def test_group_fields_by_unit_empty_input() -> None:
    assert group_fields_by_unit([]) == []


def test_unit_top_k_clamps_low_and_high() -> None:
    assert unit_top_k(1) == 20      # max(20, ...) floor
    assert unit_top_k(2) == 20      # 2*2+10 = 14 -> floored to 20
    assert unit_top_k(40) == 60     # 40*2+10 = 90 -> capped to 60
    assert unit_top_k(0) == 20


def test_unit_top_k_linear_in_the_middle() -> None:
    assert unit_top_k(10) == 30     # 10*2+10
    assert unit_top_k(9) == 28      # 9*2+10


def test_unit_needs_tables_true_for_numeric_and_list() -> None:
    for dt in ("decimal", "integer", "list"):
        [unit] = group_fields_by_unit([_fs("a", data_type=dt)])
        assert unit_needs_tables(unit) is True, dt


def test_unit_needs_tables_false_for_all_narrative_unit() -> None:
    fields = [_fs("a", data_type="string"), _fs("b", data_type="date")]
    [unit] = group_fields_by_unit(fields)
    assert unit_needs_tables(unit) is False


def test_unit_needs_tables_true_if_any_field_is_numeric() -> None:
    fields = [_fs("a", data_type="string"), _fs("b", data_type="decimal")]
    [unit] = group_fields_by_unit(fields)
    assert unit_needs_tables(unit) is True


def _chunk(
    chunk_id: str, text: str, page: int | None, heading: str | None = None
) -> dict[str, object]:
    return {"chunk_id": chunk_id, "text": text, "page": page, "heading": heading}


def test_find_consolidated_start_page_finds_auditor_report_marker() -> None:
    chunks = [
        _chunk("s1", "INDEPENDENT AUDITOR'S REPORT", 20),
        _chunk("s2", "Balance Sheet as at 31-03-2024", 60),
        _chunk("s3", "INDEPENDENT AUDITOR'S REPORT ON CONSOLIDATED FINANCIAL STATEMENTS", 133),
        _chunk("s4", "Consolidated Balance Sheet", 140),
    ]
    assert find_consolidated_start_page(chunks) == 133


def test_find_consolidated_start_page_takes_the_minimum_page() -> None:
    chunks = [
        _chunk("s1", "Consolidated Balance Sheet", 140),
        _chunk("s2", "Consolidated Statement of Profit and Loss", 133),
    ]
    assert find_consolidated_start_page(chunks) == 133


def test_find_consolidated_start_page_matches_on_heading_too() -> None:
    chunks = [_chunk("s1", "body text", 133, heading="Consolidated Balance Sheet")]
    assert find_consolidated_start_page(chunks) == 133


def test_find_consolidated_start_page_none_when_no_consolidated_section() -> None:
    chunks = [
        _chunk("s1", "Balance Sheet as at 31-03-2024", 60),
        _chunk("s2", "Statement of Profit and Loss", 62),
    ]
    assert find_consolidated_start_page(chunks) is None


def test_find_consolidated_start_page_ignores_marker_with_no_page() -> None:
    chunks = [_chunk("s1", "Consolidated Balance Sheet", None)]
    assert find_consolidated_start_page(chunks) is None


def test_find_consolidated_start_page_malformed_input_never_raises() -> None:
    assert find_consolidated_start_page([]) is None
    assert find_consolidated_start_page([{}]) is None
    assert find_consolidated_start_page([{"text": 123, "page": "x"}]) is None  # type: ignore[list-item]


def _cand(evidence_id: str, text: str, page: int | None) -> Candidate:
    return Candidate(evidence_id=evidence_id, text=text, page=page, score=1.0, source_tool="hybrid")


def test_filter_candidates_drops_at_or_after_consolidated_boundary() -> None:
    cands = [
        _cand("d:s1", "standalone", 60),
        _cand("d:s2", "consolidated", 133),
        _cand("d:s3", "consolidated later", 150),
    ]
    out = filter_candidates(cands, consolidated_start_page=133, needs_tables=True)
    assert [c.evidence_id for c in out] == ["d:s1"]


def test_filter_candidates_boundary_none_is_a_no_op() -> None:
    cands = [_cand("d:s1", "a", 60), _cand("d:s2", "b", 400)]
    out = filter_candidates(cands, consolidated_start_page=None, needs_tables=True)
    assert len(out) == 2


def test_filter_candidates_keeps_candidates_with_no_page() -> None:
    cands = [_cand("d:s1", "no page", None)]
    out = filter_candidates(cands, consolidated_start_page=133, needs_tables=True)
    assert len(out) == 1


def test_filter_candidates_drops_table_chunks_for_narrative_units() -> None:
    cands = [
        _cand("d:s1", "narrative", 10),
        _cand("d:t3", "whole table", 11),
        _cand("d:t3r7", "table row", 11),
    ]
    out = filter_candidates(cands, consolidated_start_page=None, needs_tables=False)
    assert [c.evidence_id for c in out] == ["d:s1"]


def test_filter_candidates_keeps_table_chunks_when_needs_tables() -> None:
    cands = [_cand("d:s1", "narrative", 10), _cand("d:t3r7", "table row", 11)]
    out = filter_candidates(cands, consolidated_start_page=None, needs_tables=True)
    assert len(out) == 2


def test_filter_candidates_applies_both_filters() -> None:
    cands = [
        _cand("d:s1", "keep", 10),
        _cand("d:t1r1", "table dropped", 10),
        _cand("d:s2", "page dropped", 200),
    ]
    out = filter_candidates(cands, consolidated_start_page=133, needs_tables=False)
    assert [c.evidence_id for c in out] == ["d:s1"]


def test_filter_candidates_empty_input() -> None:
    assert filter_candidates([], consolidated_start_page=133, needs_tables=True) == []
