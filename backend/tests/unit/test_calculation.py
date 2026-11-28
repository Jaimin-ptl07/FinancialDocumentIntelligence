"""The Calculation engine (S5): each Metric on hand-built facts from the
ground-truth corpus (`docs/groundtruth/*.json`), refusals with reasons, and the
comparability verdict."""

from datetime import date
from decimal import Decimal

import pytest

from fdi.core.calculation import (
    CATALOGUE,
    MetricStatus,
    calculate,
    calculate_one,
    comparability,
)
from fdi.core.financial_facts import (
    Column,
    FactStatus,
    FinancialFact,
    Observation,
    PeriodKind,
    Restatement,
    Source,
)
from fdi.core.reporting_unit import ReportingUnit

CRORE = ReportingUnit("INR", Decimal("10000000"), "INR crore")
LAKH = ReportingUnit("INR", Decimal("100000"), "INR lakh")
USD = ReportingUnit("USD", Decimal("1000000"), "USD million")
UNKNOWN = ReportingUnit(None, Decimal(1), "unknown")
MARCH = date(2026, 3, 31)


def fact(
    concept: str, value: str | None, *, fy: str = "FY2025-26", unit: ReportingUnit = CRORE,
    basis: str = "consolidated", end: date | None = MARCH, company: str = "tcs",
    restated: bool = False, status: FactStatus = FactStatus.REPORTED,
) -> FinancialFact:
    o = Observation(
        doc_id="d", doc_key="k", version=1, report_fiscal_year=fy, company_id=company,
        basis=basis, concept=concept, fiscal_year=fy, column=Column.CURRENT,
        source=Source.GRAPH_FACE, value=Decimal(value) if value is not None else None,
        printed=value, unit=unit, period_kind=PeriodKind.DURATION, period_end=end,
    )
    return FinancialFact(
        company, concept, fy, basis, status, selected=o, observations=(o,),
        restatement=Restatement(o, o, Decimal(1)) if restated else None, restated=restated,
    )


def facts(*items: FinancialFact) -> dict[tuple[str, str], FinancialFact]:
    return {(f.concept, f.fiscal_year): f for f in items}


# TCS FY2025-26 consolidated, as printed (docs/groundtruth/TCS-FY2025-26-consolidated-*).
TCS = facts(
    fact("TOTRNOTINR", "271423"), fact("PATR", "49454"), fact("PBTR", "65487"),
    fact("TOTNCR", "46667"), fact("TOTCAR", "135705"), fact("TOTNCLR", "12980"),
    fact("TOTCLR", "60914"), fact("LTOLR", "9453"), fact("STLL", "1830"),
    fact("TOTEQNR", "108478"), fact("RFOGRS", "267021"), fact("PB3NTAX", "70013"),
    fact("TOTFC", "1227"), fact("TOTOTIN", "4402"), fact("DEPEXP", "5560"),
    fact("RFOGRS", "255324", fy="FY2024-25", end=date(2025, 3, 31)),
)


def one(metric: str, data: dict[tuple[str, str], FinancialFact], *, fy: str = "FY2025-26",
        company: str = "tcs", basis: str = "consolidated"):  # type: ignore[no-untyped-def]
    return calculate_one(CATALOGUE[metric], company_id=company, fiscal_year=fy, basis=basis,
                         facts=data)


@pytest.mark.parametrize(
    ("metric", "value", "unit"),
    [
        # The S1 benchmark's calculation items, to their gold answers.
        ("net_profit_margin", "18.2203", "percent"),      # 49454 / 271423
        ("total_assets", "182372", "INR crore"),          # 46667 + 135705
        ("total_liabilities", "73894", "INR crore"),      # 12980 + 60914
        ("current_ratio", "2.2278", "ratio"),             # 135705 / 60914
        ("total_lease_liabilities", "11283", "INR crore"),
        ("effective_tax_rate", "24.4827", "percent"),     # (65487-49454)/65487 = 16033/65487
        # The design's catalogue.
        ("pat_margin", "18.5206", "percent"),             # 49454 / 267021
        ("operating_margin", "25.0310", "percent"),       # (70013+1227-4402)/267021
        ("ebitda_margin", "27.1132", "percent"),          # (+5560)
        ("return_on_equity", "45.5890", "percent"),       # 49454 / 108478
        ("revenue_growth", "4.5812", "percent"),          # 267021/255324 - 1
    ],
)
def test_each_metric_on_ground_truth_values(metric: str, value: str, unit: str) -> None:
    r = one(metric, TCS)
    assert r.status is MetricStatus.OK, r.reasons
    assert (r.value, r.unit) == (Decimal(value), unit)
    assert r.formula == CATALOGUE[metric].formula
    assert all(i.fact.fiscal_year in ("FY2025-26", "FY2024-25") for i in r.inputs)


def test_ril_revenue_net_of_gst_and_remc_effective_tax_rate_and_margin() -> None:
    ril = facts(fact("RFSP", "1043589"), fact("RFSS", "132330"), fact("GST", "100244"))
    r = one("revenue_net_of_gst", ril, company="ril")
    assert (r.status, r.value) == (MetricStatus.OK, Decimal("1075675"))
    # REMC prints current tax in brackets (as a deduction) -- PBT less PAT is
    # sign-independent and lands on the gold 21.96%.
    remc = facts(fact("PBTR", "10162.50", unit=LAKH, fy="FY2024-25"),
                 fact("PATR", "7931.09", unit=LAKH, fy="FY2024-25"),
                 fact("TOTRNOTINR", "14046.91", unit=LAKH, fy="FY2024-25"))
    tax = one("effective_tax_rate", remc, fy="FY2024-25", company="remc", basis="unknown")
    assert tax.value == Decimal("21.9573")
    npm = one("net_profit_margin", remc, fy="FY2024-25", company="remc", basis="unknown")
    assert npm.value == Decimal("56.4615")


def test_revenue_falls_back_to_gross_and_says_so() -> None:
    r = one("pat_margin", TCS)
    assert [i.concept for i in r.inputs if i.name == "revenue"] == ["RFOGRS"]
    with_net = dict(TCS)
    with_net[("RFONET", "FY2025-26")] = fact("RFONET", "260000")
    r = one("pat_margin", with_net)
    assert [i.concept for i in r.inputs if i.name == "revenue"] == ["RFONET"]


def test_a_missing_input_is_not_computable_with_the_concept_it_needed() -> None:
    r = one("debt_to_equity", TCS)
    assert r.status is MetricStatus.NOT_COMPUTABLE and r.value is None
    assert r.reasons == ("missing_input:total_borrowings:TOTBOR FY2025-26",)
    growth = one("revenue_growth", TCS, fy="FY2024-25")
    assert growth.reasons == ("missing_input:revenue_prior:RFONET|RFOGRS FY2023-24",)


def test_a_printed_nil_input_says_not_reported() -> None:
    data = dict(TCS)
    data[("TOTBOR", "FY2025-26")] = fact("TOTBOR", None, status=FactStatus.NOT_REPORTED)
    r = one("debt_to_equity", data)
    assert r.reasons == ("not_reported:total_borrowings:TOTBOR FY2025-26",)


def test_inputs_that_cannot_be_combined_are_not_comparable() -> None:
    unknown = dict(TCS)
    unknown[("TOTCLR", "FY2025-26")] = fact("TOTCLR", "60914", unit=UNKNOWN)
    assert one("current_ratio", unknown).reasons == ("unit_unknown:TOTCLR FY2025-26",)

    usd = dict(TCS)
    usd[("TOTCLR", "FY2025-26")] = fact("TOTCLR", "7000", unit=USD)
    r = one("current_ratio", usd)
    assert r.status is MetricStatus.NOT_COMPARABLE
    assert r.reasons == ("unit_mismatch:INR,USD",)

    ends = dict(TCS)
    ends[("TOTCLR", "FY2025-26")] = fact("TOTCLR", "60914", end=date(2025, 12, 31))
    assert one("current_ratio", ends).reasons == (
        "period_end_mismatch:FY2025-26:2025-12-31,2026-03-31",)


def test_ratios_across_printed_scales_are_computed_in_absolute_units() -> None:
    mixed = facts(fact("TOTCAR", "135705"), fact("TOTCLR", "6091400", unit=LAKH))
    assert one("current_ratio", mixed).value == Decimal("2.2278")
    # A sum needs one unit to be shown in: mixed scales are refused, not guessed.
    sums = facts(fact("TOTNCR", "46667"), fact("TOTCAR", "13570500", unit=LAKH))
    assert one("total_assets", sums).status is MetricStatus.NOT_COMPARABLE


def test_decimal_arithmetic_is_exact() -> None:
    r = one("total_lease_liabilities", facts(fact("LTOLR", "0.1"), fact("STLL", "0.2")))
    assert r.value == Decimal("0.3")            # never 0.30000000000000004
    assert r.normalised == Decimal("3000000.0")


def test_a_zero_denominator_is_not_computable() -> None:
    r = one("current_ratio", facts(fact("TOTCAR", "5"), fact("TOTCLR", "0")))
    assert (r.status, r.reasons) == (MetricStatus.NOT_COMPUTABLE, ("zero_denominator",))


def test_restated_inputs_are_computed_and_flagged() -> None:
    data = dict(TCS)
    data[("TOTCLR", "FY2025-26")] = fact("TOTCLR", "60914", restated=True)
    r = one("current_ratio", data)
    assert r.status is MetricStatus.OK
    assert r.warnings == ("restated_input:TOTCLR FY2025-26",)


def test_calculate_runs_each_year_in_order() -> None:
    rs = calculate(CATALOGUE["current_ratio"], company_id="tcs",
                   fiscal_years=["FY2025-26", "FY2024-25"], basis="consolidated", facts=TCS)
    assert [(r.fiscal_year, r.status) for r in rs] == [
        ("FY2025-26", MetricStatus.OK), ("FY2024-25", MetricStatus.NOT_COMPUTABLE)]


def test_comparability_verdicts() -> None:
    a = one("current_ratio", TCS)
    infy = facts(fact("TOTCAR", "90000", company="infy"), fact("TOTCLR", "40000", company="infy"))
    b = one("current_ratio", infy, company="infy")
    assert comparability(a, b, same_period=True).comparable

    standalone = facts(fact("TOTCAR", "1", basis="standalone"),
                       fact("TOTCLR", "1", basis="standalone"))
    c = one("current_ratio", standalone, basis="standalone")
    assert comparability(a, c).reasons == ("basis_mismatch:consolidated,standalone",)

    december = facts(fact("TOTCAR", "1", end=date(2025, 12, 31)),
                     fact("TOTCLR", "1", end=date(2025, 12, 31)))
    d = one("current_ratio", december)
    assert comparability(a, d).reasons == ("period_end_mismatch:2026-03-31,2025-12-31",)

    restated = dict(TCS)
    restated[("TOTCAR", "FY2025-26")] = fact("TOTCAR", "135705", restated=True)
    e = one("current_ratio", restated)
    assert comparability(a, e).reasons == ("restated_input:TOTCAR FY2025-26",)

    margin = one("pat_margin", TCS)
    assert comparability(a, margin).reasons == ("unit_mismatch:ratio,percent",)

    missing = one("debt_to_equity", TCS)
    assert "not_computable" in comparability(a, missing).reasons


def test_comparing_two_facts_across_currencies_and_one_year_apart() -> None:
    inr = fact("PATR", "49454")
    usd = fact("PATR", "5900", unit=USD, company="acme")
    assert comparability(inr, usd).reasons == ("unit_mismatch:INR,USD",)
    prior = fact("PATR", "48797", fy="FY2024-25", end=date(2025, 3, 31))
    assert comparability(inr, prior).comparable                  # one Company, two years
    assert not comparability(inr, prior, same_period=True).comparable
    unknown_end = fact("PATR", "1", end=None)
    verdict = comparability(inr, unknown_end)
    assert verdict.comparable and verdict.warnings == ("period_end_unknown",)


def test_the_catalogue_is_closed_and_well_formed() -> None:
    assert {"revenue_growth", "operating_margin", "ebitda_margin", "pat_margin",
            "return_on_equity", "debt_to_equity", "current_ratio"} <= set(CATALOGUE)
    for m in CATALOGUE.values():
        names = {i.name for i in m.inputs}
        assert {n for n, _ in (*m.numerator, *m.denominator)} == names, m.name
