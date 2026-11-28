"""The Calculation engine: a closed catalogue of Metrics over Financial facts.

FDI computes; an agent explains. Any margin, growth or ratio an agent states is
computed here, deterministically, in exact decimals, from Financial facts --
never arithmetic a model did in its head. A Metric is DATA: a name, a formula
over Concepts, a unit and a period rule. Adding one is a reviewed code change,
never a model decision.

`calculate` refuses rather than guesses. A missing input is `not_computable`,
with the Concept it needed; inputs that cannot be combined (different
currencies, an unknown scale, different period ends) are `not_comparable`,
with the reason. A gap is never silently filled.

`comparability` is the same rule set as a verdict on any two Financial facts or
Metric results -- what S8's Comparison agent returns `not_comparable` on: the
rules decide, not judgement.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, DivisionByZero, InvalidOperation, localcontext
from enum import StrEnum
from typing import Final

from fdi.core.financial_facts import FactStatus, FinancialFact, previous_fiscal_year


class MetricUnit(StrEnum):
    PERCENT = "percent"
    RATIO = "ratio"
    CURRENCY = "currency"


class MetricStatus(StrEnum):
    OK = "ok"
    NOT_COMPUTABLE = "not_computable"
    NOT_COMPARABLE = "not_comparable"


@dataclass(frozen=True)
class Input:
    """A named input: the first Concept in `concepts` that has a fact wins,
    and the choice is reported. `offset` is in Fiscal years (-1 = the year
    before the one being calculated)."""

    name: str
    concepts: tuple[str, ...]
    offset: int = 0


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    formula: str
    inputs: tuple[Input, ...]
    numerator: tuple[tuple[str, int], ...]           # (input name, +1 / -1)
    denominator: tuple[tuple[str, int], ...] = ()    # empty: no division
    unit: MetricUnit = MetricUnit.RATIO
    description: str = ""

    @property
    def multiplier(self) -> Decimal:
        return Decimal(100) if self.unit is MetricUnit.PERCENT else Decimal(1)


_REVENUE: Final = Input("revenue", ("RFONET", "RFOGRS"))
_REVENUE_NOTE: Final = (
    " Revenue is Net revenue from operations (RFONET) where printed, else Gross revenue"
    " from operations (RFOGRS): Ind AS revenue is presented net of GST, and a Report"
    " that deducts GST prints the net line."
)

# The catalogue, v1. The design's seven (§3.3, S5) plus the S1 benchmark's
# calculation items, with the S1 items' own definitions.
CATALOGUE: Final[Mapping[str, Metric]] = {m.name: m for m in (
    Metric(
        "revenue_growth", "Revenue growth",
        "(revenue[t] - revenue[t-1]) / revenue[t-1] x 100",
        (_REVENUE, Input("revenue_prior", ("RFONET", "RFOGRS"), offset=-1)),
        numerator=(("revenue", 1), ("revenue_prior", -1)),
        denominator=(("revenue_prior", 1),), unit=MetricUnit.PERCENT,
        description="Year-on-year change in revenue from operations." + _REVENUE_NOTE,
    ),
    Metric(
        "operating_margin", "Operating margin",
        "(PB3NTAX + TOTFC - TOTOTIN) / revenue x 100",
        (Input("profit_before_exceptional_and_tax", ("PB3NTAX",)),
         Input("finance_costs", ("TOTFC",)), Input("other_income", ("TOTOTIN",)), _REVENUE),
        numerator=(("profit_before_exceptional_and_tax", 1), ("finance_costs", 1),
                   ("other_income", -1)),
        denominator=(("revenue", 1),), unit=MetricUnit.PERCENT,
        description="Operating profit (EBIT excluding other income) over revenue."
        + _REVENUE_NOTE,
    ),
    Metric(
        "ebitda_margin", "EBITDA margin",
        "(PB3NTAX + TOTFC + DEPEXP - TOTOTIN) / revenue x 100",
        (Input("profit_before_exceptional_and_tax", ("PB3NTAX",)),
         Input("finance_costs", ("TOTFC",)), Input("depreciation", ("DEPEXP",)),
         Input("other_income", ("TOTOTIN",)), _REVENUE),
        numerator=(("profit_before_exceptional_and_tax", 1), ("finance_costs", 1),
                   ("depreciation", 1), ("other_income", -1)),
        denominator=(("revenue", 1),), unit=MetricUnit.PERCENT,
        description="EBITDA composed from its components (Indian statements do not print"
        " it) over revenue." + _REVENUE_NOTE,
    ),
    Metric(
        "pat_margin", "PAT margin", "PATR / revenue x 100",
        (Input("profit_after_tax", ("PATR",)), _REVENUE),
        numerator=(("profit_after_tax", 1),), denominator=(("revenue", 1),),
        unit=MetricUnit.PERCENT,
        description="Profit after tax over revenue from operations." + _REVENUE_NOTE,
    ),
    Metric(
        "net_profit_margin", "Net profit margin", "PATR / TOTRNOTINR x 100",
        (Input("profit_after_tax", ("PATR",)), Input("total_income", ("TOTRNOTINR",))),
        numerator=(("profit_after_tax", 1),), denominator=(("total_income", 1),),
        unit=MetricUnit.PERCENT,
        description="Profit after tax over total income (revenue from operations plus"
        " other income).",
    ),
    Metric(
        "effective_tax_rate", "Effective tax rate", "(PBTR - PATR) / PBTR x 100",
        (Input("profit_before_tax", ("PBTR",)), Input("profit_after_tax", ("PATR",))),
        numerator=(("profit_before_tax", 1), ("profit_after_tax", -1)),
        denominator=(("profit_before_tax", 1),), unit=MetricUnit.PERCENT,
        description="Total tax expense over profit before tax. Tax is taken as PBT less"
        " PAT, which is independent of whether the issuer prints current and deferred tax"
        " as positive expenses (TCS) or bracketed deductions (REMC).",
    ),
    Metric(
        "return_on_equity", "Return on equity", "PATR / TOTEQNR x 100",
        (Input("profit_after_tax", ("PATR",)), Input("total_equity", ("TOTEQNR",))),
        numerator=(("profit_after_tax", 1),), denominator=(("total_equity", 1),),
        unit=MetricUnit.PERCENT,
        description="Profit after tax over closing total equity.",
    ),
    Metric(
        "debt_to_equity", "Debt to equity", "TOTBOR / TOTEQNR",
        (Input("total_borrowings", ("TOTBOR",)), Input("total_equity", ("TOTEQNR",))),
        numerator=(("total_borrowings", 1),), denominator=(("total_equity", 1),),
        description="Total borrowings over closing total equity.",
    ),
    Metric(
        "current_ratio", "Current ratio", "TOTCAR / TOTCLR",
        (Input("current_assets", ("TOTCAR",)), Input("current_liabilities", ("TOTCLR",))),
        numerator=(("current_assets", 1),), denominator=(("current_liabilities", 1),),
        description="Total current assets over total current liabilities.",
    ),
    Metric(
        "total_assets", "Total assets", "TOTNCR + TOTCAR",
        (Input("non_current_assets", ("TOTNCR",)), Input("current_assets", ("TOTCAR",))),
        numerator=(("non_current_assets", 1), ("current_assets", 1)),
        unit=MetricUnit.CURRENCY,
        description="Total non-current assets plus total current assets.",
    ),
    Metric(
        "total_liabilities", "Total liabilities", "TOTNCLR + TOTCLR",
        (Input("non_current_liabilities", ("TOTNCLR",)),
         Input("current_liabilities", ("TOTCLR",))),
        numerator=(("non_current_liabilities", 1), ("current_liabilities", 1)),
        unit=MetricUnit.CURRENCY,
        description="Total non-current liabilities plus total current liabilities.",
    ),
    Metric(
        "total_lease_liabilities", "Total lease liabilities", "LTOLR + STLL",
        (Input("non_current_lease_liabilities", ("LTOLR",)),
         Input("current_lease_liabilities", ("STLL",))),
        numerator=(("non_current_lease_liabilities", 1), ("current_lease_liabilities", 1)),
        unit=MetricUnit.CURRENCY,
        description="Non-current plus current lease liabilities.",
    ),
    Metric(
        "revenue_net_of_gst", "Revenue net of GST", "RFSP + RFSS - GST",
        (Input("sale_of_products", ("RFSP",)), Input("sale_of_services", ("RFSS",)),
         Input("gst", ("GST",))),
        numerator=(("sale_of_products", 1), ("sale_of_services", 1), ("gst", -1)),
        unit=MetricUnit.CURRENCY,
        description="Value of sales plus income from services, less GST recovered.",
    ),
)}

# Percent and ratio results are shown to four places; currency sums are exact.
_PLACES: Final = Decimal("0.0001")


@dataclass(frozen=True)
class InputValue:
    name: str
    concept: str
    fiscal_year: str
    fact: FinancialFact


@dataclass(frozen=True)
class MetricResult:
    metric: str
    company_id: str
    fiscal_year: str
    basis: str
    status: MetricStatus
    value: Decimal | None
    unit: str                      # "percent", "ratio", or a currency unit label
    formula: str
    inputs: tuple[InputValue, ...]
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    normalised: Decimal | None = None   # currency Metrics: absolute units
    period_end: date | None = None


def _resolve(
    spec: Input, year: str, facts: Mapping[tuple[str, str], FinancialFact],
) -> tuple[InputValue | None, str | None]:
    """(the input's value, None) or (None, why it has none)."""
    target = year
    for _ in range(-spec.offset):
        prev = previous_fiscal_year(target)
        if prev is None:
            return None, f"missing_input:{spec.name}:no fiscal year before {target}"
        target = prev
    nil: str | None = None
    for concept in spec.concepts:
        fact = facts.get((concept, target))
        if fact is None:
            continue
        if fact.status is FactStatus.NOT_REPORTED or fact.value is None:
            nil = nil or concept
            continue
        return InputValue(spec.name, concept, target, fact), None
    wanted = "|".join(spec.concepts)
    if nil is not None:
        return None, f"not_reported:{spec.name}:{nil} {target}"
    return None, f"missing_input:{spec.name}:{wanted} {target}"


def calculate_one(
    metric: Metric, *, company_id: str, fiscal_year: str, basis: str,
    facts: Mapping[tuple[str, str], FinancialFact],
) -> MetricResult:
    """One Metric for one Company x Fiscal year x basis. `facts` maps
    (Concept, Fiscal year) to the Company's facts on that basis. Total."""
    def result(status: MetricStatus, *, value: Decimal | None = None, unit: str = "",
               inputs: Sequence[InputValue] = (), reasons: Sequence[str] = (),
               warnings: Sequence[str] = (), normalised: Decimal | None = None,
               period_end: date | None = None) -> MetricResult:
        return MetricResult(
            metric.name, company_id, fiscal_year, basis, status, value,
            unit or metric.unit.value, metric.formula, tuple(inputs), tuple(reasons),
            tuple(warnings), normalised, period_end,
        )

    resolved: dict[str, InputValue] = {}
    missing: list[str] = []
    for spec in metric.inputs:
        got, why = _resolve(spec, fiscal_year, facts)
        if got is None:
            missing.append(why or spec.name)
        else:
            resolved[spec.name] = got
    inputs = [resolved[s.name] for s in metric.inputs if s.name in resolved]
    if missing:
        return result(MetricStatus.NOT_COMPUTABLE, inputs=inputs, reasons=missing)

    reasons = _input_reasons(metric, inputs)
    warnings = [
        f"restated_input:{i.concept} {i.fiscal_year}" for i in inputs if i.fact.restated
    ] + [
        f"sources_differ:{i.concept} {i.fiscal_year}"
        for i in inputs if i.fact.disagreement is not None
    ]
    if reasons:
        return result(MetricStatus.NOT_COMPARABLE, inputs=inputs, reasons=reasons,
                      warnings=warnings)

    current = [i for i in inputs if i.fiscal_year == fiscal_year]
    period_end = next((i.fact.selected.period_end for i in current
                       if i.fact.selected.period_end is not None), None)

    def total(terms: Sequence[tuple[str, int]]) -> Decimal:
        acc = Decimal(0)
        for name, sign in terms:
            n = resolved[name].fact.normalised
            acc += (n if n is not None else Decimal(0)) * sign
        return acc

    with localcontext() as ctx:
        ctx.prec = 40
        numerator = total(metric.numerator)
        if not metric.denominator:
            unit = inputs[0].fact.unit
            value = numerator / unit.scale
            return result(MetricStatus.OK, value=value, unit=unit.label, inputs=inputs,
                          warnings=warnings, normalised=numerator, period_end=period_end)
        denominator = total(metric.denominator)
        if denominator == 0:
            return result(MetricStatus.NOT_COMPUTABLE, inputs=inputs,
                          reasons=["zero_denominator"], warnings=warnings)
        try:
            raw = numerator / denominator * metric.multiplier
        except (DivisionByZero, InvalidOperation):  # pragma: no cover - guarded above
            return result(MetricStatus.NOT_COMPUTABLE, inputs=inputs,
                          reasons=["zero_denominator"], warnings=warnings)
    value = raw.quantize(_PLACES, rounding=ROUND_HALF_EVEN)
    return result(MetricStatus.OK, value=value, inputs=inputs, warnings=warnings,
                  period_end=period_end)


def _input_reasons(metric: Metric, inputs: Sequence[InputValue]) -> list[str]:
    """Why these inputs cannot be combined, if they cannot."""
    reasons: list[str] = []
    for i in inputs:
        if not i.fact.unit.known:
            reasons.append(f"unit_unknown:{i.concept} {i.fiscal_year}")
    # An unknown unit is already a reason of its own; it is not also a currency.
    currencies = {i.fact.unit.currency for i in inputs if i.fact.unit.known}
    if len(currencies) > 1:
        reasons.append("unit_mismatch:" + ",".join(sorted(str(c) for c in currencies)))
    if metric.unit is MetricUnit.CURRENCY:
        labels = {i.fact.unit.label for i in inputs if i.fact.unit.known}
        if len(labels) > 1:
            # A sum is shown in its inputs' printed unit; mixed scales still add
            # exactly in normalised units, but there is no one unit to show.
            reasons.append("unit_mismatch:" + ",".join(sorted(labels)))
    by_year: dict[str, set[date]] = {}
    for i in inputs:
        end = i.fact.selected.period_end
        if end is not None:
            by_year.setdefault(i.fiscal_year, set()).add(end)
    for year, ends in sorted(by_year.items()):
        if len(ends) > 1:
            reasons.append(
                f"period_end_mismatch:{year}:" + ",".join(sorted(e.isoformat() for e in ends))
            )
    return reasons


def calculate(
    metric: Metric, *, company_id: str, fiscal_years: Sequence[str], basis: str,
    facts: Mapping[tuple[str, str], FinancialFact],
) -> list[MetricResult]:
    """`metric` for each Fiscal year in `fiscal_years`, in the order given."""
    return [
        calculate_one(metric, company_id=company_id, fiscal_year=fy, basis=basis, facts=facts)
        for fy in fiscal_years
    ]


# -- Comparability ------------------------------------------------------------


@dataclass(frozen=True)
class Comparable:
    """What comparability looks at, for a fact or a Metric result alike."""

    basis: str
    unit: str
    currency: str | None
    period_end: date | None
    restated: tuple[str, ...]
    computable: bool = True


@dataclass(frozen=True)
class Verdict:
    comparable: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def as_comparable(item: FinancialFact | MetricResult) -> Comparable:
    if isinstance(item, FinancialFact):
        unit = item.unit
        return Comparable(
            basis=item.basis, unit="currency" if unit.currency else unit.label,
            currency=unit.currency, period_end=item.selected.period_end,
            restated=(f"{item.concept} {item.fiscal_year}",) if item.restated else (),
            computable=item.value is not None and unit.known,
        )
    currency = next((i.fact.unit.currency for i in item.inputs), None)
    return Comparable(
        basis=item.basis,
        unit=item.unit if item.unit in ("percent", "ratio") else "currency",
        currency=currency if item.unit not in ("percent", "ratio") else None,
        period_end=item.period_end,
        restated=tuple(
            f"{i.concept} {i.fiscal_year}" for i in item.inputs if i.fact.restated
        ),
        computable=item.status is MetricStatus.OK,
    )


def comparability(
    a: FinancialFact | MetricResult, b: FinancialFact | MetricResult, *,
    same_period: bool = False,
) -> Verdict:
    """Can `a` and `b` be compared? Same basis, same unit after normalisation,
    no unresolved Restatement in an input, and comparable period ends -- the
    same month and day (a March year-end against a December one is not
    comparable), and with `same_period` (two Companies, one Fiscal year) the
    same date. Deterministic: rules, not judgement."""
    x, y = as_comparable(a), as_comparable(b)
    reasons: list[str] = []
    warnings: list[str] = []
    if not x.computable or not y.computable:
        reasons.append("not_computable")
    if x.basis != y.basis:
        reasons.append(f"basis_mismatch:{x.basis},{y.basis}")
    if x.unit != y.unit or x.currency != y.currency:
        reasons.append(
            f"unit_mismatch:{x.currency or x.unit},{y.currency or y.unit}"
        )
    for r in (*x.restated, *y.restated):
        reasons.append(f"restated_input:{r}")
    if x.period_end is None or y.period_end is None:
        warnings.append("period_end_unknown")
    elif (
        (x.period_end.month, x.period_end.day) != (y.period_end.month, y.period_end.day)
        or (same_period and x.period_end != y.period_end)
    ):
        reasons.append(
            f"period_end_mismatch:{x.period_end.isoformat()},{y.period_end.isoformat()}"
        )
    return Verdict(not reasons, tuple(reasons), tuple(warnings))
