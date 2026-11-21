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
         "(expected one of INR, INR crore, INR lakh)"),
        (_body(_gold(values=[{"value": 1.0, "unit": "INR", "tolerance": -1}])),
         "item 'fact_lookup:TCS:PATR': gold value #0 'tolerance' must be a number >= 0"),
        (_body(_gold(evidence=[{"doc_key": "tcs-2025-26", "page": 0}])),
         "item 'fact_lookup:TCS:PATR': gold evidence #0 'page' must be an integer >= 1"),
        (_body(_gold(evidence=[{"page": 3}])),
         "item 'fact_lookup:TCS:PATR': gold evidence #0 'doc_key' must be a non-empty string"),
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
