"""Financial facts and the Calculation engine over HTTP (S5).

* `GET /companies/{id}/financial-facts` -- the facts for Concepts x Fiscal
  years on one basis, each with every Observation behind it, its validation
  results and its provenance as S4 Evidence ids, plus a gap with its reason
  for every requested fact FDI has no value for.
* `GET /companies/{id}/financial-facts/{concept}/{fiscal_year}` -- one fact.
* `GET /companies/{id}/financial-history` -- the multi-year table.
* `GET /analytics/metrics` -- the Metric catalogue.
* `POST /analytics/calculate`, `/compare-periods`, `/compare-companies` --
  Metrics computed by FDI, with formula, inputs and comparability verdicts.

Values are exact decimals, serialised as strings so nothing on the way turns
them into binary floats. Everything here is a stateless read or computation;
persisting analytics results is S8's. Every Invitee may read every Company
(public filings): the access-token gate on `v1_router` is the only check.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from fdi.api.deps import SessionDep, StoreDep
from fdi.core.calculation import CATALOGUE, MetricResult, Verdict
from fdi.core.fact_projection import line_item_evidence_id
from fdi.core.financial_facts import FinancialFact, Observation
from fdi.pipeline.financial_facts import (
    FactsResult,
    FinancialFactService,
    Gap,
    observation_id,
)
from fdi.platform.db.models import Company, FinancialValidationRow

router = APIRouter(tags=["financial-facts"])

_FY = r"^FY\d{4}(-\d{2})?$"
_TAG = r"^[A-Za-z0-9_]{1,64}$"
_ID = r"^[A-Za-z0-9-]{1,64}$"
_MAX_CONCEPTS = 50
_MAX_YEARS = 20
_MAX_COMPANIES = 20


class BasisIn(StrEnum):
    CONSOLIDATED = "consolidated"
    STANDALONE = "standalone"
    UNKNOWN = "unknown"


FiscalYear = Annotated[str, Field(pattern=_FY)]
ConceptTag = Annotated[str, Field(pattern=_TAG)]


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


class UnitOut(BaseModel):
    label: str
    currency: str | None
    scale: str
    printed: str


class ObservationOut(BaseModel):
    id: str
    doc_id: str
    doc_key: str | None
    version: int | None
    report_fiscal_year: str
    column: str
    source: str
    value: str | None
    printed: str | None
    unit: UnitOut
    normalised: str | None
    period_kind: str
    period_end: str | None
    label: str
    page: int | None
    table_index: int | None
    evidence_id: str | None
    line_item_evidence_id: str | None
    validation: str
    confidence: str | None
    inputs: list[str]


class CheckOut(BaseModel):
    rule_id: str
    kind: str
    severity: str
    passed: bool
    tags: list[str]
    expected: str
    actual: str | None
    delta: str | None


class DifferenceOut(BaseModel):
    original: ObservationOut
    difference: str | None


class FactOut(BaseModel):
    concept: str
    fiscal_year: str
    basis: str
    status: str
    value: str | None
    printed: str | None
    unit: UnitOut
    normalised: str | None
    period_kind: str
    period_end: str | None
    validation: str
    restated: bool
    restatement: DifferenceOut | None
    sources_differ: DifferenceOut | None
    derived: ObservationOut | None
    evidence_ids: list[str]
    selected: ObservationOut
    observations: list[ObservationOut]
    checks: list[CheckOut]


class GapOut(BaseModel):
    concept: str
    fiscal_year: str
    basis: str
    reason: str


class FactsOut(BaseModel):
    company_id: str
    basis: str
    facts: list[FactOut]
    gaps: list[GapOut]


class HistoryCellOut(BaseModel):
    fiscal_year: str
    fact: FactOut | None
    gap: str | None


class HistoryRowOut(BaseModel):
    concept: str
    cells: list[HistoryCellOut]


class HistoryOut(BaseModel):
    company_id: str
    basis: str
    fiscal_years: list[str]
    rows: list[HistoryRowOut]


def _unit(o: Observation) -> UnitOut:
    u = o.unit
    return UnitOut(label=u.label, currency=u.currency, scale=format(u.scale, "f"),
                   printed=u.printed)


def observation_out(o: Observation) -> ObservationOut:
    return ObservationOut(
        id=observation_id(o), doc_id=o.doc_id, doc_key=o.doc_key, version=o.version,
        report_fiscal_year=o.report_fiscal_year, column=o.column.value,
        source=o.source.value, value=_dec(o.value), printed=o.printed, unit=_unit(o),
        normalised=_dec(o.normalised), period_kind=o.period_kind.value,
        period_end=o.period_end.isoformat() if o.period_end else None, label=o.label,
        page=o.page, table_index=o.table_index, evidence_id=o.evidence_id,
        line_item_evidence_id=line_item_evidence_id(o), validation=o.validation.value,
        confidence=o.confidence, inputs=list(o.inputs),
    )


def _check(r: FinancialValidationRow) -> CheckOut:
    return CheckOut(rule_id=r.rule_id, kind=r.kind, severity=r.severity, passed=r.passed,
                    tags=list(r.tags or []), expected=r.expected, actual=_dec(r.actual),
                    delta=_dec(r.delta))


def fact_out(f: FinancialFact, checks: Sequence[FinancialValidationRow] = ()) -> FactOut:
    s = f.selected
    return FactOut(
        concept=f.concept, fiscal_year=f.fiscal_year, basis=f.basis, status=f.status.value,
        value=_dec(f.value), printed=s.printed, unit=_unit(s), normalised=_dec(f.normalised),
        period_kind=s.period_kind.value,
        period_end=s.period_end.isoformat() if s.period_end else None,
        validation=s.validation.value, restated=f.restated,
        restatement=DifferenceOut(
            original=observation_out(f.restatement.original),
            difference=_dec(f.restatement.difference),
        ) if f.restatement else None,
        sources_differ=DifferenceOut(
            original=observation_out(f.disagreement.original),
            difference=_dec(f.disagreement.difference),
        ) if f.disagreement else None,
        derived=observation_out(f.derived) if f.derived else None,
        evidence_ids=sorted({o.evidence_id for o in f.observations if o.evidence_id}),
        selected=observation_out(s),
        observations=[observation_out(o) for o in f.observations],
        checks=[_check(r) for r in checks],
    )


def _gap(g: Gap) -> GapOut:
    return GapOut(concept=g.concept, fiscal_year=g.fiscal_year, basis=g.basis,
                  reason=g.reason.value)


def _facts_out(result: FactsResult) -> FactsOut:
    return FactsOut(
        company_id=result.company_id, basis=result.basis,
        facts=[fact_out(f, result.validations.get(observation_id(f.selected), ()))
               for f in result.facts],
        gaps=[_gap(g) for g in result.gaps],
    )


async def _company(session: SessionDep, company_id: str) -> None:
    if await session.get(Company, company_id) is None:
        raise HTTPException(status_code=404, detail=f"company {company_id} not found")


def _unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


CompanyId = Annotated[str, Path(pattern=_ID)]
ConceptsQ = Annotated[
    list[ConceptTag], Query(alias="concept", min_length=1, max_length=_MAX_CONCEPTS)]
YearsQ = Annotated[
    list[FiscalYear], Query(alias="fiscal_year", min_length=1, max_length=_MAX_YEARS)]
OptionalYearsQ = Annotated[
    list[FiscalYear] | None, Query(alias="fiscal_year", max_length=_MAX_YEARS)]
BasisQ = Annotated[BasisIn, Query()]


@router.get("/companies/{company_id}/financial-facts", response_model=FactsOut)
async def financial_facts(
    company_id: CompanyId, concepts: ConceptsQ, fiscal_years: YearsQ, session: SessionDep,
    store: StoreDep, basis: BasisQ = BasisIn.CONSOLIDATED,
) -> FactsOut:
    """Facts for `concept` x `fiscal_year` (both repeatable) on one basis."""
    await _company(session, company_id)
    service = FinancialFactService(session=session, store=store)
    result = await service.facts(company_id=company_id, concepts=_unique(concepts),
                                 fiscal_years=_unique(fiscal_years), basis=basis.value)
    return _facts_out(result)


@router.get(
    "/companies/{company_id}/financial-facts/{concept}/{fiscal_year}", response_model=FactOut
)
async def financial_fact(
    company_id: CompanyId,
    concept: Annotated[str, Path(pattern=_TAG)],
    fiscal_year: Annotated[str, Path(pattern=_FY)],
    session: SessionDep, store: StoreDep, basis: BasisQ = BasisIn.CONSOLIDATED,
) -> FactOut:
    """One fact with every Observation behind it. 404 with the gap's reason
    when FDI has no value for it."""
    await _company(session, company_id)
    service = FinancialFactService(session=session, store=store)
    fact, gap, checks = await service.fact(
        company_id=company_id, concept=concept, fiscal_year=fiscal_year, basis=basis.value)
    if fact is None:
        reason = gap.reason.value if gap is not None else "not_found"
        raise HTTPException(status_code=404, detail={
            "message": f"no {basis.value} {concept} for {fiscal_year}: {reason}",
            "gap": reason})
    return fact_out(fact, checks)


@router.get("/companies/{company_id}/financial-history", response_model=HistoryOut)
async def financial_history(
    company_id: CompanyId, concepts: ConceptsQ, session: SessionDep, store: StoreDep,
    fiscal_years: OptionalYearsQ = None, basis: BasisQ = BasisIn.CONSOLIDATED,
) -> HistoryOut:
    """A multi-year table: one row per Concept, one cell per Fiscal year
    (every year the Company's Reports cover, unless `fiscal_year` narrows
    it), oldest first."""
    await _company(session, company_id)
    service = FinancialFactService(session=session, store=store)
    wanted = _unique(concepts)
    result = await service.history(
        company_id=company_id, concepts=wanted, basis=basis.value,
        fiscal_years=_unique(fiscal_years) if fiscal_years else None)
    years = sorted({f.fiscal_year for f in result.facts} | {g.fiscal_year for g in result.gaps})
    facts = {(f.concept, f.fiscal_year): f for f in result.facts}
    gaps = {(g.concept, g.fiscal_year): g for g in result.gaps}
    return HistoryOut(
        company_id=company_id, basis=basis.value, fiscal_years=years,
        rows=[HistoryRowOut(concept=c, cells=[
            HistoryCellOut(
                fiscal_year=fy,
                fact=fact_out(facts[(c, fy)], result.validations.get(
                    observation_id(facts[(c, fy)].selected), ()))
                if (c, fy) in facts else None,
                gap=gaps[(c, fy)].reason.value if (c, fy) in gaps else None,
            ) for fy in years
        ]) for c in wanted],
    )


# -- analytics ----------------------------------------------------------------


class MetricOut(BaseModel):
    name: str
    label: str
    formula: str
    unit: str
    inputs: list[dict[str, object]]
    description: str


class MetricInputOut(BaseModel):
    name: str
    concept: str
    fiscal_year: str
    value: str | None
    unit: str
    normalised: str | None
    status: str
    evidence_ids: list[str]


class MetricResultOut(BaseModel):
    metric: str
    company_id: str
    fiscal_year: str
    basis: str
    status: str
    value: str | None
    unit: str
    formula: str
    inputs: list[MetricInputOut]
    reasons: list[str]
    warnings: list[str]
    normalised: str | None
    period_end: str | None


class VerdictOut(BaseModel):
    comparable: bool
    reasons: list[str]
    warnings: list[str]


class CalculateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str = Field(pattern=_TAG)
    company_id: str = Field(pattern=_ID)
    fiscal_years: list[FiscalYear] = Field(min_length=1, max_length=_MAX_YEARS)
    basis: BasisIn = BasisIn.CONSOLIDATED


class CompareCompaniesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str = Field(pattern=_TAG)
    company_ids: list[Annotated[str, Field(pattern=_ID)]] = Field(
        min_length=2, max_length=_MAX_COMPANIES)
    fiscal_year: FiscalYear
    basis: BasisIn = BasisIn.CONSOLIDATED


class CalculationOut(BaseModel):
    results: list[MetricResultOut]


class ComparisonOut(BaseModel):
    results: list[MetricResultOut]
    comparisons: list[VerdictOut]


def result_out(r: MetricResult) -> MetricResultOut:
    return MetricResultOut(
        metric=r.metric, company_id=r.company_id, fiscal_year=r.fiscal_year, basis=r.basis,
        status=r.status.value, value=_dec(r.value), unit=r.unit, formula=r.formula,
        inputs=[MetricInputOut(
            name=i.name, concept=i.concept, fiscal_year=i.fiscal_year,
            value=_dec(i.fact.value), unit=i.fact.unit.label,
            normalised=_dec(i.fact.normalised), status=i.fact.status.value,
            evidence_ids=sorted({o.evidence_id for o in i.fact.observations if o.evidence_id}),
        ) for i in r.inputs],
        reasons=list(r.reasons), warnings=list(r.warnings), normalised=_dec(r.normalised),
        period_end=r.period_end.isoformat() if r.period_end else None,
    )


def _verdict(v: Verdict) -> VerdictOut:
    return VerdictOut(comparable=v.comparable, reasons=list(v.reasons), warnings=list(v.warnings))


def _known_metric(name: str) -> None:
    if name not in CATALOGUE:
        raise HTTPException(status_code=422, detail=f"unknown metric {name!r}; see "
                            "GET /api/v1/analytics/metrics")


@router.get("/analytics/metrics", response_model=list[MetricOut])
async def metrics() -> list[MetricOut]:
    """The Metric catalogue: what FDI computes, and from what."""
    return [
        MetricOut(name=m.name, label=m.label, formula=m.formula, unit=m.unit.value,
                  inputs=[{"name": i.name, "concepts": list(i.concepts),
                           "fiscal_year_offset": i.offset} for i in m.inputs],
                  description=m.description)
        for m in CATALOGUE.values()
    ]


@router.post("/analytics/calculate", response_model=CalculationOut)
async def calculate_metric(
    body: CalculateIn, session: SessionDep, store: StoreDep,
) -> CalculationOut:
    """A Metric for a Company in each requested Fiscal year: value, formula,
    inputs with their sources, or why it could not be computed."""
    _known_metric(body.metric)
    await _company(session, body.company_id)
    service = FinancialFactService(session=session, store=store)
    results = await service.calculate(
        metric=body.metric, company_id=body.company_id,
        fiscal_years=_unique(body.fiscal_years), basis=body.basis.value)
    return CalculationOut(results=[result_out(r) for r in results])


@router.post("/analytics/compare-periods", response_model=ComparisonOut)
async def compare_periods(
    body: CalculateIn, session: SessionDep, store: StoreDep,
) -> ComparisonOut:
    """A Metric across a Company's Fiscal years (oldest first), with a
    comparability verdict for each consecutive pair."""
    _known_metric(body.metric)
    await _company(session, body.company_id)
    service = FinancialFactService(session=session, store=store)
    results, verdicts = await service.compare_periods(
        metric=body.metric, company_id=body.company_id,
        fiscal_years=_unique(body.fiscal_years), basis=body.basis.value)
    return ComparisonOut(results=[result_out(r) for r in results],
                         comparisons=[_verdict(v) for v in verdicts])


@router.post("/analytics/compare-companies", response_model=ComparisonOut)
async def compare_companies(
    body: CompareCompaniesIn, session: SessionDep, store: StoreDep,
) -> ComparisonOut:
    """One Metric for several Companies in one Fiscal year, with each
    Company's comparability verdict against the first."""
    _known_metric(body.metric)
    company_ids = _unique(body.company_ids)
    for company_id in company_ids:
        await _company(session, company_id)
    service = FinancialFactService(session=session, store=store)
    results, verdicts = await service.compare_companies(
        metric=body.metric, company_ids=company_ids, fiscal_year=body.fiscal_year,
        basis=body.basis.value)
    return ComparisonOut(results=[result_out(r) for r in results],
                         comparisons=[_verdict(v) for v in verdicts])
