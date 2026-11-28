"""The Financial-fact rule (S5): which fact an Invitee gets for given
Observations -- never how the rule got there."""

from datetime import date
from decimal import Decimal

import pytest

from fdi.core.financial_facts import (
    Column,
    FactStatus,
    GapReason,
    Observation,
    PeriodKind,
    Source,
    concepts_from_schema,
    gap_reason,
    previous_fiscal_year,
    previous_period_end,
    rounding_tolerance,
    select_fact,
    select_facts,
)
from fdi.core.reporting_unit import ReportingUnit

CRORE = ReportingUnit("INR", Decimal("10000000"), "INR crore", "(₹ crore)")
LAKH = ReportingUnit("INR", Decimal("100000"), "INR lakh", "(₹ in lakhs)")
UNKNOWN = ReportingUnit(None, Decimal(1), "unknown")


def obs(
    value: str | None, *, report_fy: str = "FY2025-26", fy: str = "FY2025-26",
    column: Column = Column.CURRENT, source: Source = Source.GRAPH_FACE,
    doc_key: str = "acme-2026", version: int = 1, basis: str = "consolidated",
    concept: str = "PATR", unit: ReportingUnit = CRORE, label: str = "Profit for the year",
    company: str = "acme",
) -> Observation:
    return Observation(
        doc_id=f"{doc_key}-v{version}", doc_key=doc_key, version=version,
        report_fiscal_year=report_fy, company_id=company, basis=basis, concept=concept,
        fiscal_year=fy, column=column, source=source,
        value=Decimal(value) if value is not None else None,
        printed=value, unit=unit, period_kind=PeriodKind.DURATION,
        period_end=date(2026, 3, 31), label=label,
    )


def comparative_of_next_report(value: str, **kw: object) -> Observation:
    """FY2024-25 as printed in the comparative column of the FY2025-26 Report."""
    return obs(value, report_fy="FY2025-26", fy="FY2024-25", column=Column.COMPARATIVE,
               doc_key="acme-2026", **kw)  # type: ignore[arg-type]


def current_of_own_report(value: str, **kw: object) -> Observation:
    """FY2024-25 as printed in the current column of its own Report."""
    return obs(value, report_fy="FY2024-25", fy="FY2024-25", doc_key="acme-2025",
               **kw)  # type: ignore[arg-type]


def test_a_single_report_gives_its_printed_value() -> None:
    fact = select_fact([obs("49454")])
    assert fact is not None
    assert (fact.status, fact.value, fact.unit.label) == (
        FactStatus.REPORTED, Decimal("49454"), "INR crore")
    assert fact.normalised == Decimal("494540000000")
    assert not fact.restated


def test_current_and_next_reports_comparative_agreeing_is_not_a_restatement() -> None:
    fact = select_fact([current_of_own_report("48797"), comparative_of_next_report("48797")])
    assert fact is not None
    assert fact.status is FactStatus.REPORTED and not fact.restated
    assert fact.selected.doc_key == "acme-2026"       # the latest Report states it
    assert len(fact.observations) == 2                # both kept


def test_a_later_report_restating_the_same_face_line_wins_and_keeps_the_original() -> None:
    fact = select_fact([current_of_own_report("48797"), comparative_of_next_report("48553")])
    assert fact is not None
    assert fact.status is FactStatus.RESTATED and fact.restated
    assert fact.value == Decimal("48553")
    assert fact.restatement is not None
    assert fact.restatement.original.value == Decimal("48797")
    assert fact.restatement.original.doc_key == "acme-2025"
    assert fact.restatement.difference == Decimal("-2440000000")   # -244 crore, normalised


def test_a_rounding_only_difference_between_units_is_not_a_restatement() -> None:
    # 2,55,324 crore vs 2,55,32,412.34 lakh: the same number printed at two
    # precisions -- within half a printed step of each (half a crore plus
    # half of 0.01 lakh).
    crore = current_of_own_report("255324")
    lakh = comparative_of_next_report("25532412.34", unit=LAKH)
    fact = select_fact([crore, lakh])
    assert fact is not None and not fact.restated and fact.disagreement is None
    assert rounding_tolerance(crore, lakh) == Decimal("5000500")


def test_different_lines_disagreeing_are_recorded_not_called_a_restatement() -> None:
    fact = select_fact([
        current_of_own_report("18177", source=Source.GRAPH_NOTE, label="Dividend paid"),
        comparative_of_next_report("12", source=Source.GRAPH_NOTE, label="Dividend paid"),
    ])
    assert fact is not None
    assert fact.status is FactStatus.REPORTED and not fact.restated
    assert fact.disagreement is not None and fact.disagreement.original.value == Decimal("18177")
    face = select_fact([
        current_of_own_report("100", label="Loans"),
        comparative_of_next_report("90", label="Loans to employees"),
    ])
    assert face is not None and not face.restated and face.disagreement is not None


def test_an_invitee_correction_wins_over_everything() -> None:
    fact = select_fact([
        obs("49454"),
        obs("49500", source=Source.EXTRACTION_CORRECTED),
        obs("49454", source=Source.GRAPH_NOTE),
    ])
    assert fact is not None
    assert (fact.status, fact.value) == (FactStatus.CORRECTED, Decimal("49500"))


@pytest.mark.parametrize(
    ("sources", "winner"),
    [
        ((Source.GRAPH_NOTE, Source.GRAPH_FACE, Source.GRAPH_COMPOSED), Source.GRAPH_FACE),
        ((Source.GRAPH_COMPOSED, Source.GRAPH_NOTE), Source.GRAPH_NOTE),
        ((Source.GRAPH_COMPOSED, Source.EXTRACTION_ACCEPTED), Source.EXTRACTION_ACCEPTED),
        ((Source.EXTRACTION_ACCEPTED, Source.GRAPH_NOTE), Source.GRAPH_NOTE),
    ],
)
def test_face_before_note_before_accepted_before_composed(
    sources: tuple[Source, ...], winner: Source
) -> None:
    fact = select_fact([obs(str(100 + i), source=s) for i, s in enumerate(sources)])
    assert fact is not None and fact.selected.source is winner


def test_a_composed_value_stands_beside_the_printed_total() -> None:
    fact = select_fact([obs("1227"), obs("1057", source=Source.GRAPH_COMPOSED)])
    assert fact is not None and fact.status is FactStatus.REPORTED
    assert fact.derived is not None and fact.derived.value == Decimal("1057")


def test_composed_only_is_a_composed_fact() -> None:
    fact = select_fact([obs("70013", source=Source.GRAPH_COMPOSED)])
    assert fact is not None and fact.status is FactStatus.COMPOSED and fact.derived is None


def test_a_printed_nil_alone_is_not_reported_and_never_beats_a_value() -> None:
    nil = obs(None, label="Exceptional items")
    fact = select_fact([nil])
    assert fact is not None and fact.status is FactStatus.NOT_REPORTED and fact.value is None
    valued = select_fact([nil, obs("5", source=Source.GRAPH_NOTE)])
    assert valued is not None and valued.value == Decimal("5")


def test_a_superseded_version_never_takes_part() -> None:
    fact = select_fact([obs("900", version=1), obs("1000", version=2)])
    assert fact is not None
    assert fact.value == Decimal("1000") and not fact.restated
    assert {o.version for o in fact.observations} == {2}


def test_standalone_and_consolidated_are_never_merged() -> None:
    facts = select_facts([obs("49454"), obs("49096", basis="standalone")])
    assert [(f.basis, f.value) for f in facts] == [
        ("consolidated", Decimal("49454")), ("standalone", Decimal("49096"))]


def test_unknown_units_are_never_compared_across_scales() -> None:
    fact = select_fact([current_of_own_report("100", unit=UNKNOWN),
                        comparative_of_next_report("100000", unit=CRORE)])
    assert fact is not None and not fact.restated and fact.disagreement is None


def test_select_facts_is_deterministic_regardless_of_input_order() -> None:
    items = [current_of_own_report("48797"), comparative_of_next_report("48553"), obs("49454")]
    assert select_facts(items) == select_facts(list(reversed(items)))


@pytest.mark.parametrize(
    ("label", "expected"),
    [("FY2025-26", "FY2024-25"), ("FY2000-01", "FY1999-00"), ("FY2025", "FY2024"),
     ("2025-26", None), (None, None)],
)
def test_previous_fiscal_year(label: str | None, expected: str | None) -> None:
    assert previous_fiscal_year(label) == expected


def test_previous_period_end_handles_a_leap_day() -> None:
    assert previous_period_end(date(2024, 2, 29)) == date(2023, 2, 28)
    assert previous_period_end(date(2026, 3, 31)) == date(2025, 3, 31)
    assert previous_period_end(None) is None


def test_concepts_take_their_period_kind_from_their_statement() -> None:
    schema = {"modules": [
        {"module": "Profit and Loss", "retrieval_units": [{"fields": [
            {"tag": "PATR", "field_name": "Profit (loss) After Tax"}]}]},
        {"module": "Assets", "retrieval_units": [{"fields": [
            {"tag": "TOTCAR", "field_name": "Total Current Assets",
             "fields": [{"tag": "INVT", "field_name": "Inventories"}]}]}]},
    ]}
    concepts = concepts_from_schema(schema)
    assert concepts["PATR"].period_kind is PeriodKind.DURATION
    assert concepts["TOTCAR"].period_kind is PeriodKind.INSTANT
    assert concepts["INVT"].period_kind is PeriodKind.INSTANT
    assert concepts_from_schema({}) == {}


def test_gap_reasons() -> None:
    assert gap_reason(report_exists=False, processed=False, missed=False) is GapReason.NO_REPORT
    assert gap_reason(report_exists=True, processed=False, missed=False) is (
        GapReason.NOT_PROCESSED)
    assert gap_reason(report_exists=True, processed=True, missed=True) is GapReason.MISSED
    assert gap_reason(report_exists=True, processed=True, missed=False) is GapReason.NOT_FOUND
