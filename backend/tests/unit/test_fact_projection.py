"""Projecting one Report's graph into Observations (S5): what Observations an
Invitee's Report yields, as printed, with unit, period and validation."""

from datetime import date
from decimal import Decimal

from fdi.core.compose import Formula
from fdi.core.fact_projection import (
    ExtractedValue,
    ReportIdentity,
    line_item_evidence_id,
    project_report,
)
from fdi.core.financial_facts import (
    Column,
    Concept,
    Observation,
    PeriodKind,
    Source,
    Validation,
)

DOC = "0b6f1a52-9b3c-4f3e-8a55-2d6c1f1e7a10"
REPORT = ReportIdentity(DOC, "acme-2026", 1, "acme", "FY2025-26", date(2026, 3, 31))
ST = "st:consolidated:profit_and_loss"
NT = "nt:consolidated:13"

FACE = [
    ["", "Note", "Year ended March 31, 2026", "Year ended March 31, 2025"],
    ["Revenue from operations", "12", "2,67,021", "2,55,324"],
    ["Other income", "13", "4,402", "3,962"],
    ["TOTAL INCOME", "", "2,71,423", "2,59,286"],
    ["Exceptional items", "", "-", "-"],
    ["Earnings per equity share (₹)", "", "136.01", "134.19"],
]
NOTE = [
    ["Particulars", "2026", "2025"],
    ["Interest income", "3,000.50", "2,500"],
    ["Dividend income", "1,401.50", "1,462"],
]
BEFORE_FACE = "Consolidated Statement of Profit and Loss\n(₹ crore)\n"
BEFORE_NOTE = "\n13. Other income\n(₹ crore)\n"


def _table(rows: list[list[str]], offset: int) -> dict[str, object]:
    return {
        "spans": [{"offset": offset, "length": 40}],
        "cells": [{"row_index": r, "column_index": c, "content": text}
                  for r, row in enumerate(rows) for c, text in enumerate(row)],
    }


def _ocr() -> dict[str, object]:
    face_at = len(BEFORE_FACE)
    note_at = face_at + 200 + len(BEFORE_NOTE)
    content = BEFORE_FACE + ("x" * 200) + BEFORE_NOTE + ("y" * 200)
    return {"content": content, "tables": [_table(FACE, face_at), _table(NOTE, note_at)]}


def _item(i: int, label: str, current: float | None, prior: float | None, *,
          parent: str = ST, origin: str = "face", table: int = 0, row: int,
          kind: str = "currency") -> dict[str, object]:
    return {"id": f"li:{parent}:r{i}", "parent": parent, "origin": origin, "label": label,
            "caption": "", "ref": "", "current": current, "prior": prior, "page": 4,
            "table_index": table, "row_index": i, "source_row": row, "kind": kind}


FOG = {"bases": {"consolidated": {
    "statements": [{"id": ST, "statement": "profit_and_loss", "basis": "consolidated",
                    "page": 4, "table_index": 0}],
    "line_items": [
        _item(0, "Revenue from operations", 267021.0, 255324.0, row=1),
        _item(1, "Other income", 4402.0, 3962.0, row=2),
        _item(2, "TOTAL INCOME", 271423.0, 259286.0, row=3),
        _item(3, "Exceptional items", None, None, row=4),
        _item(4, "Earnings per equity share (₹)", 136.01, 134.19, row=5, kind="per_share"),
        _item(0, "Interest income", 3000.5, 2500.0, parent=NT, origin="note", table=1, row=1),
        _item(1, "Dividend income", 1401.5, 1462.0, parent=NT, origin="note", table=1, row=2),
        _item(2, "Unmapped line", 7.0, 6.0, parent=NT, origin="note", table=1, row=2),
    ],
    "edges": [],
}}}


def _maps(*pairs: tuple[str, str]) -> dict[str, object]:
    return {"bases": {"consolidated": {
        "edges": [{"type": "MAPS_TO", "source": s, "target": t} for s, t in pairs],
        "proof": [{"line_item": s, "method": "lexical", "score": 1.0} for s, _ in pairs],
    }}}


MAPPING = _maps(
    (f"li:{ST}:r0", "RFOGRS"), (f"li:{ST}:r1", "TOTOTIN"), (f"li:{ST}:r2", "TOTRNOTINR"),
    (f"li:{ST}:r3", "EOI"), (f"li:{ST}:r4", "EPSB"),
    (f"li:{NT}:r0", "INTOI"), (f"li:{NT}:r1", "DIVOI"), (f"li:{NT}:r2", "NOTACONCEPT"),
)
REASONING = {"bases": {"consolidated": {
    "checks": [
        {"rule_id": "sum:TOTRNOTINR", "kind": "reconciliation", "severity": "error",
         "passed": True, "tags": ["TOTRNOTINR", "RFOGRS"], "expected": "271423",
         "actual": 271423.0, "delta": 0.0, "tolerance": 1.0},
        {"rule_id": "domain:EPSB", "kind": "domain", "severity": "warn", "passed": False,
         "tags": ["EPSB"], "expected": ">= 0", "actual": 136.01, "delta": 0.0},
        {"rule_id": "sum:RFOGRS", "kind": "reconciliation", "severity": "warn",
         "passed": False, "tags": ["RFOGRS"], "expected": "x", "actual": 1.0},
    ],
    "confidence": {"RFOGRS": {"band": "high"}},
    "coverage": {"not_answered": ["PATR"]},
}}}
P_AND_L = PeriodKind.DURATION
CONCEPTS = {t: Concept(t, t.title(), "Profit and Loss", P_AND_L) for t in (
    "RFOGRS", "TOTOTIN", "TOTRNOTINR", "EOI", "EPSB", "INTOI", "DIVOI", "TOTOTING", "PATR")}
FORMULAS = {
    "TOTOTING": Formula("TOTOTING", "derivation", (("INTOI", 1.0), ("DIVOI", 1.0))),
    "TOTOTIN": Formula("TOTOTIN", "derivation", (("TOTOTING", 1.0),)),
    "TOTRNOTINR": Formula("TOTRNOTINR", "reconciliation", (("RFOGRS", 1.0),)),
}


def project(**kw: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "fog": FOG, "mapping": MAPPING, "reasoning": REASONING, "ocr": _ocr(),
        "concepts": CONCEPTS, "formulas": FORMULAS,
    }
    args.update(kw)
    return project_report(REPORT, **args)  # type: ignore[arg-type]


def find(observations: tuple[Observation, ...], concept: str, column: Column = Column.CURRENT,
         source: Source | None = None) -> Observation:
    [o] = [o for o in observations if o.concept == concept and o.column is column
           and (source is None or o.source is source)]
    return o


def test_a_face_line_is_observed_as_printed_in_both_columns_with_its_unit_and_period() -> None:
    obs = project().observations
    cur = find(obs, "RFOGRS")
    assert (cur.value, cur.printed, cur.unit.label) == (Decimal("267021"), "2,67,021", "INR crore")
    assert cur.normalised == Decimal("2670210000000")
    assert (cur.fiscal_year, cur.period_end, cur.source) == (
        "FY2025-26", date(2026, 3, 31), Source.GRAPH_FACE)
    assert cur.evidence_id == f"{DOC}:fact:consolidated:RFOGRS"
    assert line_item_evidence_id(cur) == f"{DOC}:li:st:consolidated:profit_and_loss:r0"
    prior = find(obs, "RFOGRS", Column.COMPARATIVE)
    assert (prior.value, prior.printed, prior.fiscal_year, prior.period_end) == (
        Decimal("255324"), "2,55,324", "FY2024-25", date(2025, 3, 31))


def test_a_note_line_keeps_its_printed_decimals() -> None:
    o = find(project().observations, "INTOI")
    assert (o.source, o.value, o.printed) == (Source.GRAPH_NOTE, Decimal("3000.50"), "3,000.50")
    assert str(o.value) == "3000.50"


def test_per_share_lines_are_in_rupees_whatever_the_statement_says() -> None:
    o = find(project().observations, "EPSB")
    assert (o.value, o.unit.label, o.unit.scale) == (Decimal("136.01"), "INR", Decimal(1))


def test_a_printed_nil_is_an_observation_with_no_value() -> None:
    o = find(project().observations, "EOI")
    assert (o.value, o.printed) == (None, "-")
    assert not [x for x in project().observations
                if x.concept == "EOI" and x.column is Column.COMPARATIVE]


def test_subtotals_are_composed_in_exact_decimals_beside_and_instead_of_print() -> None:
    obs = project().observations
    standing_in = find(obs, "TOTOTING", source=Source.GRAPH_COMPOSED)
    assert standing_in.value == Decimal("4402.00")          # 3,000.50 + 1,401.50
    assert standing_in.inputs == ("INTOI", "DIVOI")
    assert standing_in.unit.label == "INR crore" and standing_in.printed is None
    beside = find(obs, "TOTOTIN", source=Source.GRAPH_COMPOSED)
    assert beside.value == Decimal("4402.00")
    assert find(obs, "TOTOTIN", source=Source.GRAPH_FACE).value == Decimal("4402")
    # A reconciliation is a check, never a composed value.
    assert not [o for o in obs if o.concept == "TOTRNOTINR" and o.source is Source.GRAPH_COMPOSED]
    prior = find(obs, "TOTOTING", Column.COMPARATIVE, Source.GRAPH_COMPOSED)
    assert prior.value == Decimal("3962")


def test_validation_and_confidence_come_from_the_reports_checks() -> None:
    result = project()
    obs = result.observations
    assert find(obs, "TOTRNOTINR").validation is Validation.PASSED
    assert find(obs, "EPSB").validation is Validation.FAILED
    assert find(obs, "RFOGRS").validation is Validation.FAILED     # one failed check sinks it
    assert find(obs, "INTOI").validation is Validation.UNCHECKED
    assert find(obs, "RFOGRS").confidence == "high"
    assert [v.rule_id for v in result.validations] == [
        "domain:EPSB", "sum:RFOGRS", "sum:TOTRNOTINR"]
    assert result.validations[2].actual == Decimal("271423.0")


def test_reviewed_extracted_values_become_observations() -> None:
    extracted = [
        ExtractedValue("read-1", "PATR", "accepted", {"current": 49454, "prior": 48797.5},
                       "consolidated", "Profit after tax"),
        ExtractedValue("read-2", "RFOGRS", "corrected", 267000, "consolidated"),
        ExtractedValue("read-3", "NOTACONCEPT", "accepted", 5, "consolidated"),
        ExtractedValue("read-4", "PATR", "accepted", 1, None),
    ]
    result = project(extracted=extracted)
    obs = result.observations
    patr = find(obs, "PATR", source=Source.EXTRACTION_ACCEPTED)
    assert (patr.value, patr.unit.label, patr.extracted_value_id) == (
        Decimal("49454"), "INR crore", "read-1")
    assert find(obs, "PATR", Column.COMPARATIVE).value == Decimal("48797.5")
    assert find(obs, "RFOGRS", source=Source.EXTRACTION_CORRECTED).value == Decimal("267000")
    assert result.diagnostics["consolidated"]["extraction_unknown_concept"] == 1  # type: ignore[index]
    assert result.diagnostics["extraction_basis_unknown"] == 1


def test_lines_mapped_to_no_concept_are_counted_not_stored() -> None:
    result = project()
    assert not [o for o in result.observations if o.concept == "NOTACONCEPT"]
    diag = result.diagnostics["consolidated"]
    assert diag["unknown_concept"] == 1  # type: ignore[index]
    assert diag["not_answered"] == ["PATR"]  # type: ignore[index]


def test_printed_text_that_does_not_match_the_graph_is_never_used() -> None:
    fog = {"bases": {"consolidated": {**FOG["bases"]["consolidated"], "line_items": [  # type: ignore[index]
        _item(0, "Revenue from operations", 999.0, None, row=1)]}}}
    result = project(fog=fog)
    o = find(result.observations, "RFOGRS")
    assert (o.value, o.printed) == (Decimal("999.0"), None)
    assert result.diagnostics["consolidated"]["printed_unverified"] == 1  # type: ignore[index]


def test_a_report_without_a_unit_line_is_unknown_not_guessed() -> None:
    ocr = _ocr()
    ocr["content"] = str(ocr["content"]).replace("(₹ crore)", "(see notes)")
    o = find(project(ocr=ocr).observations, "RFOGRS")
    assert not o.unit.known and o.normalised is None


def test_projection_is_deterministic_and_total() -> None:
    assert project() == project()
    empty = project_report(REPORT, fog={}, mapping={}, reasoning={}, ocr={},
                           concepts=CONCEPTS, formulas={})
    assert empty.observations == () and empty.validations == ()
    junk = project_report(REPORT, fog={"bases": {"consolidated": {"line_items": [None, 3]}}},
                          mapping={"bases": "x"}, reasoning={"bases": []}, ocr={"tables": 7},
                          concepts=CONCEPTS, formulas=FORMULAS)
    assert junk.observations == ()


def test_identities_are_unique() -> None:
    obs = project().observations
    assert len({o.identity for o in obs}) == len(obs)
