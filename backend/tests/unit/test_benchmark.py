"""The benchmark parser rejects every malformed item at load, naming the item
and the reason -- a typo must never silently score as a miss."""

import copy

import pytest

from fdi.core.benchmark import (
    BenchmarkError,
    Evidence,
    GoldValue,
    fact_lookup_benchmark,
    parse_benchmark,
)

_ITEM = {
    "id": "fact_lookup:TCS:PATR",
    "kind": "fact_lookup",
    "question": "What was TCS's profit after tax?",
    "doc_keys": ["tcs-2025-26"],
    "gold": {
        "answerable": True,
        "values": [{"value": 48797.0, "unit": "INR crore", "tolerance": 0.5}],
        "evidence": [{"doc_key": "tcs-2025-26", "page": 167}],
    },
}


def _body(*items: object) -> dict[str, object]:
    return {"version": "v0", "items": list(items) if items else [copy.deepcopy(_ITEM)]}


def _item(**patch: object) -> dict[str, object]:
    item = copy.deepcopy(_ITEM)
    item.update(patch)
    return item


def _gold(**patch: object) -> dict[str, object]:
    gold = copy.deepcopy(_ITEM["gold"])
    assert isinstance(gold, dict)
    gold.update(patch)
    return _item(gold=gold)


def test_a_well_formed_item_parses() -> None:
    bench = parse_benchmark(_body())
    assert bench.version == "v0"
    item = bench.items[0]
    assert item.id == "fact_lookup:TCS:PATR"
    assert item.doc_keys == ("tcs-2025-26",)
    assert item.gold.values[0].value == 48797.0
    assert item.gold.evidence[0].page == 167


def test_expected_tools_are_optional_and_parse_in_order() -> None:
    assert parse_benchmark(_body()).items[0].gold.expected_tools is None
    bench = parse_benchmark(_body(_gold(expected_tools=["search", "get_tables"])))
    assert bench.items[0].gold.expected_tools == ("search", "get_tables")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ([], "benchmark must be a JSON object"),
        ({"items": []}, "benchmark 'version' must be a non-empty string"),
        ({"version": "v0", "items": {}}, "benchmark 'items' must be a list"),
        (_body("nope"), "item #0: must be a JSON object"),
        (_body(_item(id="")), "item #0: 'id' must be a non-empty string"),
        (_body(_item(), _item()), "item 'fact_lookup:TCS:PATR': duplicate id"),
        (_body(_item(kind="trivia")), "item 'fact_lookup:TCS:PATR': unknown kind 'trivia'"),
        (_body(_item(question=" ")),
         "item 'fact_lookup:TCS:PATR': 'question' must be a non-empty string"),
        (_body(_item(doc_keys="tcs")),
         "item 'fact_lookup:TCS:PATR': 'doc_keys' must be a list of non-empty strings"),
        (_body(_item(gold=None)), "item 'fact_lookup:TCS:PATR': 'gold' must be a JSON object"),
        (_body(_gold(answerable="yes")),
         "item 'fact_lookup:TCS:PATR': 'gold.answerable' must be true or false"),
        (_body(_gold(values=[])),
         "item 'fact_lookup:TCS:PATR': a fact_lookup item needs at least one gold value"),
        (_body(_gold(answerable=False)),
         "item 'fact_lookup:TCS:PATR': a fact_lookup item must be answerable"),
        (_body(_gold(values=[{"value": True, "unit": "INR crore"}])),
         "item 'fact_lookup:TCS:PATR': gold value #0 'value' must be a number"),
        (_body(_gold(values=[{"value": 1.0, "unit": "USD"}])),
         "item 'fact_lookup:TCS:PATR': gold value #0 unknown unit 'USD' "
         "(expected one of INR, INR crore, INR lakh, percent, ratio)"),
        (_body(_gold(values=[{"value": 1.0, "unit": "INR", "tolerance": -1}])),
         "item 'fact_lookup:TCS:PATR': gold value #0 'tolerance' must be a number >= 0"),
        (_body(_gold(evidence=[{"doc_key": "tcs-2025-26", "page": 0}])),
         "item 'fact_lookup:TCS:PATR': gold evidence #0 'page' must be an integer >= 1"),
        (_body(_gold(evidence=[{"page": 3}])),
         "item 'fact_lookup:TCS:PATR': gold evidence #0 'doc_key' must be a non-empty string"),
        (_body(_gold(expected_tools="search")),
         "item 'fact_lookup:TCS:PATR': 'gold.expected_tools' must be a non-empty list "
         "of tool names"),
        (_body(_gold(expected_tools=[])),
         "item 'fact_lookup:TCS:PATR': 'gold.expected_tools' must be a non-empty list "
         "of tool names"),
        (_body(_gold(expected_tools=["search", " "])),
         "item 'fact_lookup:TCS:PATR': 'gold.expected_tools' #1 must be a non-empty string"),
        (_body(_gold(expected_tools=["search", 7])),
         "item 'fact_lookup:TCS:PATR': 'gold.expected_tools' #1 must be a non-empty string"),
        (_body(_gold(expected_tools=["search", "get_tables", "search"])),
         "item 'fact_lookup:TCS:PATR': 'gold.expected_tools' names 'search' twice"),
    ],
)
def test_a_malformed_benchmark_is_rejected_with_its_reason(body: object, reason: str) -> None:
    with pytest.raises(BenchmarkError) as exc:
        parse_benchmark(body)  # type: ignore[arg-type]
    assert str(exc.value) == reason


# --------------------------------------------------------------------------
# fact_lookup generation from docs/groundtruth/-shaped bodies.
# --------------------------------------------------------------------------

_TCS_GT = {
    "issuer": "TCS",
    "fiscal_year": "FY2025-26",
    "basis": "consolidated",
    "statement": "profit_and_loss",
    "_source": "Consolidated Statement of Profit and Loss, PDF page 167 of annual-report.pdf.",
    "_unit": "INR crore, exactly as printed. No rescaling. Brackets are negative.",
    "values": {"PATR": 48797.0, "EPSB": 134.19},
    "not_reported": ["EXCHEXP"],
}
_REMC_GT = {
    "issuer": "REMC",
    "fiscal_year": "FY2024-25",
    "basis": "unknown",
    "statement": "profit_and_loss",
    "_source": "Statement of Profit and Loss, PDF page 61 of Annual_Report.pdf (REMC Limited).",
    "_unit": "INR LAKH, exactly as printed.",
    "values": {"CT": -2681.75},
    "not_reported": [],
}
_LABELS = {
    "PATR": "Profit (loss) After Tax",
    "EPSB": "Basic earnings per share",
    "CT": "Current tax",
    "EXCHEXP": "Exchange loss",
}


def test_every_ground_truth_value_becomes_one_fact_lookup_item() -> None:
    body = fact_lookup_benchmark("v0", [_TCS_GT, _REMC_GT], _LABELS)
    bench = parse_benchmark(body)

    assert [i.id for i in bench.items] == [
        "fact_lookup:REMC-FY2024-25-unknown-profit_and_loss:CT",
        "fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:EPSB",
        "fact_lookup:TCS-FY2025-26-consolidated-profit_and_loss:PATR",
    ]
    remc, eps, patr = bench.items
    assert patr.kind == "fact_lookup"
    assert patr.question == (
        "What was TCS's consolidated Profit (loss) After Tax for FY2025-26, as reported?"
    )
    assert patr.doc_keys == ("tcs-2025-26",)
    assert patr.gold.values[0] == GoldValue(value=48797.0, unit="INR crore", tolerance=0.5)
    assert patr.gold.evidence == (Evidence(doc_key="tcs-2025-26", page=167),)

    # A per-share figure is in rupees, whatever the statement's unit.
    assert eps.gold.values[0] == GoldValue(value=134.19, unit="INR", tolerance=0.005)

    # REMC keeps its real name in the question and its historical doc_key;
    # an unknown basis is simply not mentioned.
    assert remc.question == "What was REMC's Current tax for FY2024-25, as reported?"
    assert "RITES" not in remc.question
    assert remc.doc_keys == ("rites-2024-25",)
    assert remc.gold.values[0] == GoldValue(value=-2681.75, unit="INR lakh", tolerance=0.005)
    assert remc.gold.evidence == (Evidence(doc_key="rites-2024-25", page=61),)


def test_generation_is_stable_whatever_order_the_files_arrive_in() -> None:
    assert fact_lookup_benchmark("v0", [_TCS_GT, _REMC_GT], _LABELS) == fact_lookup_benchmark(
        "v0", [_REMC_GT, _TCS_GT], _LABELS
    )


def test_a_numbered_cross_reference_is_dropped_from_the_label() -> None:
    labels = {**_LABELS, "PATR": "Profit (loss) for period (11 plus/minus 12 & 13)"}
    bench = parse_benchmark(fact_lookup_benchmark("v0", [_TCS_GT], labels))
    assert bench.items[1].question == (
        "What was TCS's consolidated Profit (loss) for period for FY2025-26, as reported?"
    )


def test_an_as_reported_qualifier_is_not_repeated_in_the_question() -> None:
    labels = {**_LABELS, "PATR": "Total Current Assets (as reported on the face)"}
    bench = parse_benchmark(fact_lookup_benchmark("v0", [_TCS_GT], labels))
    assert bench.items[1].question == (
        "What was TCS's consolidated Total Current Assets for FY2025-26, as reported?"
    )


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"values": {"PATR": 1.0}, "not_reported": ["PATR"]},
         "ground truth #0 does not parse (see fdi.core.scoring.parse_groundtruth)"),
        ({"issuer": "INFY"}, "ground truth INFY-FY2025-26-consolidated-profit_and_loss: "
                             "no doc_key for issuer 'INFY'"),
        ({"_source": "Consolidated P&L."}, "ground truth TCS-FY2025-26-consolidated-"
                                           "profit_and_loss: '_source' names no PDF page"),
        ({"_unit": "USD million"}, "ground truth TCS-FY2025-26-consolidated-"
                                   "profit_and_loss: '_unit' names no INR unit"),
        ({"values": {"ZZZ": 1.0}}, "ground truth TCS-FY2025-26-consolidated-"
                                   "profit_and_loss: tag 'ZZZ' has no label in the schema"),
    ],
)
def test_ground_truth_that_cannot_make_an_item_is_rejected(
    patch: dict[str, object], reason: str
) -> None:
    with pytest.raises(BenchmarkError) as exc:
        fact_lookup_benchmark("v0", [{**_TCS_GT, **patch}], _LABELS)
    assert str(exc.value) == reason


# --------------------------------------------------------------------------
# Per-kind field rules: each kind names the fields it cannot be scored without.
# --------------------------------------------------------------------------

_CROSS = {
    "id": "cross_company:q1",
    "kind": "cross_company",
    "question": "Compare TCS's and RIL's basic EPS.",
    "doc_keys": ["tcs-2025-26", "ril-2025-26"],
    "gold": {
        "answerable": True,
        "values": [{"value": 136.01, "unit": "INR"}, {"value": 19.59, "unit": "INR"}],
        "evidence": [{"doc_key": "tcs-2025-26", "page": 167},
                     {"doc_key": "ril-2025-26", "page": 145}],
    },
}
_CALC = {
    "id": "calculation:q1",
    "kind": "calculation",
    "question": "What was TCS's total tax expense?",
    "doc_keys": ["tcs-2025-26"],
    "gold": {
        "answerable": True,
        "formula": "CT + DT = 16388 + (-355) = 16033",
        "values": [{"value": 16033.0, "unit": "INR crore"}],
        "evidence": [{"doc_key": "tcs-2025-26", "page": 167}],
    },
}
_UNANSWERABLE = {
    "id": "unanswerable:q1",
    "kind": "unanswerable",
    "question": "What was Infosys's revenue for FY2025-26?",
    "gold": {"answerable": False},
}
# No Company has two ingested Reports yet (S2); the schema accepts the kind now.
_MULTI_YEAR = {
    "id": "multi_year:q1",
    "kind": "multi_year",
    "question": "How did TCS's basic EPS change from FY2025-26 to FY2026-27?",
    "doc_keys": ["tcs-2025-26", "tcs-2026-27"],
    "gold": {
        "answerable": True,
        "values": [{"value": 136.01, "unit": "INR"}, {"value": 140.0, "unit": "INR"}],
        "evidence": [{"doc_key": "tcs-2025-26", "page": 167}],
    },
}


def _patched(base: dict[str, object], gold: dict[str, object] | None = None,
             **patch: object) -> dict[str, object]:
    """`base` with top-level fields replaced and gold fields merged; a gold
    field patched to None is removed."""
    item = copy.deepcopy(base)
    item.update(patch)
    if gold is not None:
        merged = copy.deepcopy(item["gold"])
        assert isinstance(merged, dict)
        merged.update(gold)
        item["gold"] = {k: v for k, v in merged.items() if v is not None}
    return item


def test_well_formed_items_of_every_hand_keyed_kind_parse() -> None:
    bench = parse_benchmark(_body(_CROSS, _CALC, _UNANSWERABLE, _MULTI_YEAR))
    cross, calc, unanswerable, multi = bench.items
    assert cross.doc_keys == ("tcs-2025-26", "ril-2025-26")
    assert [v.value for v in cross.gold.values] == [136.01, 19.59]
    assert calc.gold.formula == "CT + DT = 16388 + (-355) = 16033"
    assert unanswerable.gold.answerable is False
    assert unanswerable.gold.values == ()
    assert multi.kind == "multi_year"
    assert len(multi.gold.values) == 2


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (_patched(_CROSS, doc_keys=["tcs-2025-26"]),
         "item 'cross_company:q1': a cross_company item needs at least two doc_keys "
         "(one per Company compared)"),
        (_patched(_CROSS, doc_keys=["tcs-2025-26", "tcs-2025-26"]),
         "item 'cross_company:q1': a cross_company item needs at least two doc_keys "
         "(one per Company compared)"),
        (_patched(_CROSS, gold={"values": []}),
         "item 'cross_company:q1': a cross_company item needs at least one gold value"),
        (_patched(_CROSS, gold={"answerable": False}),
         "item 'cross_company:q1': a cross_company item must be answerable"),
        (_patched(_CALC, gold={"formula": None}),
         "item 'calculation:q1': a calculation item must record the 'gold.formula' "
         "its gold value was derived by"),
        (_patched(_CALC, gold={"formula": " "}),
         "item 'calculation:q1': 'gold.formula' must be a non-empty string"),
        (_patched(_CALC, gold={"values": []}),
         "item 'calculation:q1': a calculation item needs at least one gold value"),
        (_patched(_CALC, gold={"answerable": False}),
         "item 'calculation:q1': a calculation item must be answerable"),
        (_patched(_UNANSWERABLE, gold={"answerable": True}),
         "item 'unanswerable:q1': an unanswerable item must have 'gold.answerable' false"),
        (_patched(_UNANSWERABLE, gold={"values": [{"value": 1.0, "unit": "INR"}]}),
         "item 'unanswerable:q1': an unanswerable item has no gold values"),
        (_patched(_UNANSWERABLE, gold={"evidence": [{"doc_key": "tcs-2025-26", "page": 1}]}),
         "item 'unanswerable:q1': an unanswerable item has no gold evidence"),
        (_patched(_MULTI_YEAR, gold={"values": []}),
         "item 'multi_year:q1': a multi_year item needs at least one gold value"),
    ],
)
def test_a_hand_keyed_item_missing_what_its_kind_needs_is_rejected(
    item: dict[str, object], reason: str
) -> None:
    with pytest.raises(BenchmarkError) as exc:
        parse_benchmark(_body(item))
    assert str(exc.value) == reason


_RESEARCH = {
    "id": "research:q1",
    "kind": "research",
    "question": "What does the note behind TCS's other income say it consists of?",
    "doc_keys": ["tcs-2025-26"],
    "gold": {
        "answerable": True,
        "values": [{"value": 3962.0, "unit": "INR crore"}],
        "evidence": [{"doc_key": "tcs-2025-26", "page": 210}],
        "required_claims": [
            "Interest income is the largest component of other income.",
            "Other income includes net gain on investments.",
        ],
    },
}


def test_a_research_item_parses_with_its_required_claims() -> None:
    item = parse_benchmark(_body(_RESEARCH)).items[0]
    assert item.gold.required_claims == (
        "Interest income is the largest component of other income.",
        "Other income includes net gain on investments.",
    )
    assert item.gold.evidence == (Evidence("tcs-2025-26", 210),)
    assert item.gold.values == (GoldValue(3962.0, "INR crore", 0.5),)


def test_a_narrative_research_item_needs_no_gold_values() -> None:
    item = parse_benchmark(_body(_patched(_RESEARCH, gold={"values": None}))).items[0]
    assert item.gold.values == ()


def test_items_of_other_kinds_have_no_required_claims() -> None:
    assert parse_benchmark(_body()).items[0].gold.required_claims == ()


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (_patched(_RESEARCH, gold={"answerable": False}),
         "item 'research:q1': a research item must be answerable"),
        (_patched(_RESEARCH, gold={"evidence": None}),
         "item 'research:q1': a research item needs at least one gold evidence location"),
        (_patched(_RESEARCH, gold={"required_claims": None}),
         "item 'research:q1': a research item needs at least one required claim"),
        (_patched(_RESEARCH, gold={"required_claims": []}),
         "item 'research:q1': 'gold.required_claims' must be a non-empty list of claims"),
        (_patched(_RESEARCH, gold={"required_claims": "Interest is largest."}),
         "item 'research:q1': 'gold.required_claims' must be a non-empty list of claims"),
        (_patched(_RESEARCH, gold={"required_claims": ["ok", " "]}),
         "item 'research:q1': 'gold.required_claims' #1 must be a non-empty string"),
        (_patched(_RESEARCH, gold={"required_claims": ["same", "same"]}),
         "item 'research:q1': 'gold.required_claims' states 'same' twice"),
        (_patched(_UNANSWERABLE, gold={"required_claims": ["Infosys is not ingested."]}),
         "item 'unanswerable:q1': an unanswerable item has no required claims"),
    ],
)
def test_a_malformed_research_item_is_rejected(item: dict[str, object], reason: str) -> None:
    with pytest.raises(BenchmarkError) as exc:
        parse_benchmark(_body(item))
    assert str(exc.value) == reason
