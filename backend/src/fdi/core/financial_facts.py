"""Observations, and the one rule that picks a Financial fact from them (S5).

An **Observation** is one value a source states for a Concept, a Fiscal year
and a basis: a Report's statement face, one of its notes, a subtotal FDI
composed from printed components, or an Invitee's accepted or corrected
Extracted value. The same Fiscal year is usually observed twice -- as the
current column of its own Report and as the comparative column of the next
one -- and when the issuer restates, the two disagree.

A **Financial fact** is what FDI shows for Company x Concept x Fiscal year x
basis. `select_facts` is the single function that decides it:

1. an Invitee-corrected Observation wins;
2. otherwise the latest Report (by its Fiscal year, then version) that states
   the value, face before note before an accepted Extracted value before
   composed;
3. if the SAME statement-face line, printed by two different Reports, differs
   by more than the rounding of its printed precision, the fact is
   `restated`: both are kept and the original is one hop away. Any other
   cross-Report difference -- a note row, or two different lines the graph
   mapped to one Concept -- is recorded as a `disagreement`, never presented
   as a restatement. MEASURED on TCS FY2024-25 vs FY2025-26: every face line
   agreed, while 58 facts differed between the Reports, all of them note rows
   whose label repeats across notes ("Loans", "Others", "Dividend paid" 12 vs
   18,177). Calling those restatements would put an issuer's name to FDI's own
   mapping noise;
4. if only a composed Observation exists, the fact is `composed`;
5. if the only Observations are printed nils, the fact is `not_reported` --
   the issuer printed the line and said nothing is there.

Superseded Document versions never take part: a Report re-uploaded as v2
replaces v1, and the two disagreeing is a correction, not a restatement.
Standalone and consolidated are separate facts by construction -- basis is
part of the grouping key, so nothing can merge them.

Values are exact decimals as printed. Nothing here touches a binary float.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Final

from fdi.core.numbers import printed_places
from fdi.core.reporting_unit import ReportingUnit


class Column(StrEnum):
    """Which column of its Report an Observation was printed in."""

    CURRENT = "current"
    COMPARATIVE = "comparative"


class Source(StrEnum):
    GRAPH_FACE = "graph_face"
    GRAPH_NOTE = "graph_note"
    GRAPH_COMPOSED = "graph_composed"
    EXTRACTION_ACCEPTED = "extraction_accepted"
    EXTRACTION_CORRECTED = "extraction_corrected"


# Within one Report, which source states a value best. Printed beats read
# beats computed; an Invitee correction is outside this order (rule 1).
_SOURCE_RANK: Final[Mapping[str, int]] = {
    Source.GRAPH_FACE: 4,
    Source.GRAPH_NOTE: 3,
    Source.EXTRACTION_ACCEPTED: 2,
    Source.GRAPH_COMPOSED: 1,
}
PRINTED_SOURCES: Final = frozenset({Source.GRAPH_FACE, Source.GRAPH_NOTE})


class FactStatus(StrEnum):
    REPORTED = "reported"
    COMPOSED = "composed"
    RESTATED = "restated"
    CORRECTED = "corrected"
    NOT_REPORTED = "not_reported"


class PeriodKind(StrEnum):
    """`duration` for income and cash-flow lines (a year's flow), `instant`
    for balance-sheet lines (as at the period end)."""

    DURATION = "duration"
    INSTANT = "instant"


class Validation(StrEnum):
    """Did the Report's reconciliation checks that involve this value pass?"""

    PASSED = "passed"
    FAILED = "failed"
    UNCHECKED = "unchecked"


class GapReason(StrEnum):
    """Why a requested Company x Concept x Fiscal year x basis has no fact."""

    NO_REPORT = "no_report"            # no Report of the Company covers the year
    NOT_PROCESSED = "not_processed"    # a Report covers it, its facts are not projected yet
    MISSED = "missed"                  # FDI asked and got no answer: our defect
    NOT_FOUND = "not_found"            # FDI found no line for it; may be undisclosed


@dataclass(frozen=True)
class Concept:
    """A taxonomy Concept. Seeded from the FDI standard schema; never created
    by a model."""

    tag: str
    name: str
    statement: str
    period_kind: PeriodKind


@dataclass(frozen=True)
class Observation:
    """One value one source states for Concept x Fiscal year x basis.

    `value` is None only for a printed nil (`-`). `normalised` is the value in
    absolute units (rupees), None when the unit is unknown."""

    doc_id: str
    doc_key: str | None
    version: int | None
    report_fiscal_year: str
    company_id: str
    basis: str
    concept: str
    fiscal_year: str
    column: Column
    source: Source
    value: Decimal | None
    printed: str | None
    unit: ReportingUnit
    period_kind: PeriodKind
    period_end: date | None = None
    label: str = ""
    line_item: str | None = None
    page: int | None = None
    table_index: int | None = None
    evidence_id: str | None = None
    validation: Validation = Validation.UNCHECKED
    confidence: str | None = None
    inputs: tuple[str, ...] = ()
    extracted_value_id: str | None = None

    @property
    def identity(self) -> tuple[str, str, str, str, str, str]:
        """Report version x basis x Concept x Fiscal year x column x source --
        what makes two Observations the same one."""
        return (self.doc_id, self.basis, self.concept, self.fiscal_year,
                self.column.value, self.source.value)

    @property
    def normalised(self) -> Decimal | None:
        if self.value is None or not self.unit.known:
            return None
        return self.value * self.unit.scale

    @property
    def report_key(self) -> str:
        """Which Report this came from, across its versions."""
        return self.doc_key or self.doc_id


@dataclass(frozen=True)
class Restatement:
    """A later Report printed a different value for the same fact."""

    latest: Observation
    original: Observation
    difference: Decimal | None      # latest - original, normalised


@dataclass(frozen=True)
class FinancialFact:
    company_id: str
    concept: str
    fiscal_year: str
    basis: str
    status: FactStatus
    selected: Observation
    observations: tuple[Observation, ...]
    restatement: Restatement | None = None
    derived: Observation | None = None
    restated: bool = False
    # Reports stated different values for this fact, but not on the same face
    # line -- so it is recorded, not called a restatement.
    disagreement: Restatement | None = None

    @property
    def value(self) -> Decimal | None:
        return self.selected.value

    @property
    def unit(self) -> ReportingUnit:
        return self.selected.unit

    @property
    def normalised(self) -> Decimal | None:
        return self.selected.normalised

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.company_id, self.concept, self.fiscal_year, self.basis)


# -- Fiscal years -----------------------------------------------------------

_FY_RANGE: Final = re.compile(r"^FY(\d{4})-(\d{2})$")
_FY_SINGLE: Final = re.compile(r"^FY(\d{4})$")


def previous_fiscal_year(label: str | None) -> str | None:
    """The Fiscal year before `label` in the same notation: FY2025-26 ->
    FY2024-25, FY2025 -> FY2024. None for anything else. Total."""
    if not isinstance(label, str):
        return None
    m = _FY_RANGE.match(label)
    if m:
        start = int(m.group(1)) - 1
        return f"FY{start}-{(start + 1) % 100:02d}"
    m = _FY_SINGLE.match(label)
    if m:
        return f"FY{int(m.group(1)) - 1}"
    return None


def previous_period_end(end: date | None) -> date | None:
    """The same day a year earlier; 29 February becomes the 28th."""
    if end is None:
        return None
    try:
        return end.replace(year=end.year - 1)
    except ValueError:
        return end.replace(year=end.year - 1, day=28)


# -- The rule ---------------------------------------------------------------


def rounding_tolerance(a: Observation, b: Observation) -> Decimal:
    """How far apart two printed values may be and still be the same number
    rounded differently, in normalised units: half a printed step of each.
    `2,67,021` crore and `26,70,21,045.12` lakh can differ by up to half a
    crore and agree."""
    total = Decimal(0)
    for o in (a, b):
        if o.value is None:
            continue
        step = Decimal(1).scaleb(-printed_places(o.value))
        total += step * (o.unit.scale if o.unit.known else Decimal(1))
    return total / 2


def _differ(a: Observation, b: Observation) -> Decimal | None:
    """The normalised difference a - b when it exceeds rounding, else None.
    Two values in different, or unknown, units are compared only when their
    unit labels match -- never across an unknown scale."""
    if a.value is None or b.value is None:
        return None
    if a.unit.known and b.unit.known:
        x, y = a.normalised, b.normalised
    elif a.unit.label == b.unit.label:
        x, y = a.value, b.value
    else:
        return None
    if x is None or y is None:
        return None
    diff = x - y
    return diff if abs(diff) > rounding_tolerance(a, b) else None


def _report_order(o: Observation) -> tuple[str, int]:
    return (o.report_fiscal_year, o.version or 0)


def _latest_versions(observations: Iterable[Observation]) -> list[Observation]:
    """Only each Report's latest Document version takes part."""
    obs = list(observations)
    latest: dict[str, int] = {}
    for o in obs:
        v = o.version or 0
        if v > latest.get(o.report_key, -1):
            latest[o.report_key] = v
    return [o for o in obs if (o.version or 0) == latest[o.report_key]]


def _sort_key(o: Observation) -> tuple[object, ...]:
    """A total, deterministic order: newest Report first, best source first."""
    return (
        tuple(-ord(c) for c in o.report_fiscal_year), -(o.version or 0),
        -_SOURCE_RANK.get(o.source, 0), o.column.value, o.doc_id,
    )


def select_fact(observations: Sequence[Observation]) -> FinancialFact | None:
    """The Financial fact for ONE Company x Concept x Fiscal year x basis
    group, or None for an empty group. See the module docstring for the rule.
    Total."""
    live = _latest_versions(observations)
    if not live:
        return None
    ordered = sorted(live, key=_sort_key)
    first = ordered[0]
    company, concept, year, basis = (
        first.company_id, first.concept, first.fiscal_year, first.basis,
    )
    valued = [o for o in ordered if o.value is not None]

    corrected = [o for o in valued if o.source is Source.EXTRACTION_CORRECTED]
    ranked = [o for o in valued if o.source is not Source.EXTRACTION_CORRECTED]
    selected: Observation
    if corrected:
        selected = corrected[0]
    elif ranked:
        selected = ranked[0]
    else:
        # Only printed nils: the issuer printed the line and stated nothing.
        return FinancialFact(
            company, concept, year, basis, FactStatus.NOT_REPORTED,
            selected=ordered[0], observations=tuple(ordered),
        )

    restatement, disagreement = _cross_report([o for o in valued if o.source in PRINTED_SOURCES])
    derived = next(
        (o for o in valued
         if o.source is Source.GRAPH_COMPOSED and o.doc_id == selected.doc_id
         and o is not selected),
        None,
    )
    if selected.source is Source.EXTRACTION_CORRECTED:
        status = FactStatus.CORRECTED
    elif restatement is not None:
        status = FactStatus.RESTATED
    elif selected.source is Source.GRAPH_COMPOSED:
        status = FactStatus.COMPOSED
    else:
        status = FactStatus.REPORTED
    return FinancialFact(
        company, concept, year, basis, status, selected=selected,
        observations=tuple(ordered), restatement=restatement,
        derived=derived if selected.source is not Source.GRAPH_COMPOSED else None,
        restated=restatement is not None,
        disagreement=disagreement,
    )


_LABEL_NOISE: Final = re.compile(r"[^a-z0-9]+")


def _same_line(a: Observation, b: Observation) -> bool:
    """Are two Observations the same printed line of a statement face? Only
    then can a difference be the issuer's restatement rather than FDI reading
    two different lines."""
    return (
        a.source is Source.GRAPH_FACE and b.source is Source.GRAPH_FACE
        and _LABEL_NOISE.sub(" ", a.label.lower()).strip()
        == _LABEL_NOISE.sub(" ", b.label.lower()).strip()
    )


def _cross_report(
    printed: Sequence[Observation],
) -> tuple[Restatement | None, Restatement | None]:
    """(restatement, disagreement): the latest Report's printed value set
    against the earliest other Report's that differs from it beyond rounding.
    A difference on the same face line is a restatement; any other is a
    disagreement. Observations of ONE Report never restate each other."""
    if not printed:
        return None, None
    by_report: dict[str, Observation] = {}
    for o in sorted(printed, key=_sort_key):        # best per Report first
        by_report.setdefault(o.report_key, o)
    if len(by_report) < 2:
        return None, None
    reports = sorted(by_report.values(), key=_report_order)
    latest = reports[-1]
    disagreement: Restatement | None = None
    for earlier in reports[:-1]:                   # earliest first
        diff = _differ(latest, earlier)
        if diff is None:
            continue
        found = Restatement(latest=latest, original=earlier, difference=diff)
        if _same_line(latest, earlier):
            return found, None
        disagreement = disagreement or found
    return None, disagreement


def select_facts(observations: Iterable[Observation]) -> list[FinancialFact]:
    """Every Financial fact the Observations support, one per Company x
    Concept x Fiscal year x basis, in a stable order. Total."""
    groups: dict[tuple[str, str, str, str], list[Observation]] = {}
    for o in observations:
        groups.setdefault((o.company_id, o.concept, o.fiscal_year, o.basis), []).append(o)
    out: list[FinancialFact] = []
    for key in sorted(groups):
        fact = select_fact(groups[key])
        if fact is not None:
            out.append(fact)
    return out


# -- Concepts ---------------------------------------------------------------

_DURATION_STATEMENTS: Final = frozenset({"profit and loss", "cash flow"})


def concepts_from_schema(schema: Mapping[str, object]) -> dict[str, Concept]:
    """Every tagged Field of a v4.3 standard schema document as a Concept.
    A module's statement decides the period kind: profit and loss and cash
    flow lines are durations, everything else an instant. The first
    declaration of a tag wins. Total."""
    out: dict[str, Concept] = {}
    modules = schema.get("modules") if isinstance(schema, Mapping) else None

    def walk(node: object, statement: str, kind: PeriodKind) -> None:
        if isinstance(node, Mapping):
            tag = node.get("tag")
            if isinstance(tag, str) and tag and tag not in out:
                name = node.get("field_name")
                out[tag] = Concept(
                    tag, name if isinstance(name, str) and name else tag, statement, kind,
                )
            for v in node.values():
                if isinstance(v, list | Mapping):
                    walk(v, statement, kind)
        elif isinstance(node, list):
            for v in node:
                walk(v, statement, kind)

    for module in modules if isinstance(modules, list) else []:
        if not isinstance(module, Mapping):
            continue
        raw = module.get("module") or module.get("name") or ""
        statement = str(raw)
        kind = (PeriodKind.DURATION if statement.strip().lower() in _DURATION_STATEMENTS
                else PeriodKind.INSTANT)
        walk(module, statement, kind)
    return out


def gap_reason(
    *, report_exists: bool, processed: bool, missed: bool
) -> GapReason:
    """Why a requested fact is absent, from what is known about its Report."""
    if not report_exists:
        return GapReason.NO_REPORT
    if not processed:
        return GapReason.NOT_PROCESSED
    if missed:
        return GapReason.MISSED
    return GapReason.NOT_FOUND

