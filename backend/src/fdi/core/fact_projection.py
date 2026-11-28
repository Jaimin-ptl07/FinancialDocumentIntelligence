"""Project one Report's financial object graph into Observations (S5, ADR-0006).

The per-Report graph artifacts stay the record of HOW each value was derived
(line items, edges, arbitration, checks). This module turns them into the
cross-Report Observations the Financial fact service stores, and it is the
only producer of those rows: drop the tables and re-run it, and the same rows
come back byte for byte.

What one Report contributes, per basis:

* the arbitrated winner for every mapped Concept (`fog_reason.fact_claims`,
  the same winner the reasoning stage and `score_pipeline.py` use, so the
  projection is lossless by construction), as a `graph_face` or `graph_note`
  Observation for the current column and, where the row prints one, the
  comparative column;
* a printed nil (`-`) on a mapped line that no valued line claims, as a
  value-less Observation -- the issuer said "nothing", which is not the same
  as FDI finding nothing;
* every subtotal whose every input is stated, composed in exact decimals, as
  `graph_composed` -- beside a printed total, or standing in for an unprinted
  one;
* every Invitee-accepted or corrected Extracted value whose Field names a
  Concept (S3).

THE PRINTED TEXT COMES FROM THE OCR, NOT THE GRAPH. The graph stores floats.
Rather than re-build the graph (and re-pay its model-mapped stage) to carry
strings, the projection reads the very cells the graph parsed its floats from
(`statements.printed_amounts`) and keeps the text only when it parses back to
the graph's value -- so the two can never describe different cells.

Pure (`fdi.core` law): no I/O, deterministic, total.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from fdi.core.compose import Formula
from fdi.core.evidence import fact_ref, ref_for_fog_node
from fdi.core.financial_facts import (
    Column,
    Concept,
    Observation,
    Source,
    Validation,
    previous_fiscal_year,
    previous_period_end,
)
from fdi.core.fog_reason import fact_claims
from fdi.core.numbers import is_nil_marker, parse_amount, parse_decimal
from fdi.core.quantity import classify_quantity
from fdi.core.reporting_unit import (
    UNKNOWN,
    ReportingUnit,
    column_period_ends,
    table_unit,
    unit_for_kind,
)
from fdi.core.statements import printed_amounts, table_rows

# Bumped whenever the rows this module emits for the same artifacts change, so
# a stored projection made by an older version is re-made (`projection_key`).
PROJECTION_VERSION: Final = 1
_MAX_COMPOSE_PASSES: Final = 12


@dataclass(frozen=True)
class ReportIdentity:
    """What the projection needs to know about the Report it reads."""

    doc_id: str
    doc_key: str | None
    version: int | None
    company_id: str
    fiscal_year: str
    period_end: date | None = None


@dataclass(frozen=True)
class ExtractedValue:
    """An Invitee-reviewed Extracted value (S3) for a Field that names a
    Concept. `value` is a number or a {current, prior} pair, in the Report's
    own reporting unit."""

    id: str
    tag: str
    action: str           # "accepted" | "corrected"
    value: object
    basis: str | None
    label: str = ""


@dataclass(frozen=True)
class ValidationResult:
    """One of the Report's reconciliation or domain checks, as the reasoning
    stage recorded it."""

    doc_id: str
    basis: str
    rule_id: str
    kind: str
    severity: str
    passed: bool
    tags: tuple[str, ...]
    expected: str
    actual: Decimal | None
    delta: Decimal | None
    tolerance: Decimal | None


@dataclass(frozen=True)
class Projection:
    observations: tuple[Observation, ...]
    validations: tuple[ValidationResult, ...]
    diagnostics: Mapping[str, object] = field(default_factory=dict)


def _region(doc: object, basis: str) -> Mapping[str, object]:
    bases = doc.get("bases") if isinstance(doc, Mapping) else None
    region = bases.get(basis) if isinstance(bases, Mapping) else None
    return region if isinstance(region, Mapping) else {}


def _bases(doc: object) -> list[str]:
    bases = doc.get("bases") if isinstance(doc, Mapping) else None
    return sorted(b for b in bases if isinstance(b, str)) if isinstance(bases, Mapping) else []


def _decimal(value: object) -> Decimal | None:
    """A JSON number as an exact decimal of its shortest repr -- never the
    binary expansion of a float. None for anything that is not a finite
    number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        d = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _table(ocr: Mapping[str, object], index: object) -> Mapping[str, object] | None:
    tables = ocr.get("tables") if isinstance(ocr, Mapping) else None
    if not isinstance(tables, list) or not isinstance(index, int) or isinstance(index, bool):
        return None
    if not (0 <= index < len(tables)):
        return None
    t = tables[index]
    return t if isinstance(t, Mapping) else None


def _header_dates(table: Mapping[str, object] | None) -> tuple[date, ...]:
    if table is None:
        return ()
    rows = table_rows(table)
    for r in sorted(rows)[:3]:
        got = column_period_ends([rows[r][c] for c in sorted(rows[r])])
        if got:
            return got
    return ()


def _validation_by_tag(checks: object) -> dict[str, Validation]:
    """A tag's validation status: failed when any check on it failed, passed
    when at least one passed and none failed, otherwise unchecked."""
    out: dict[str, Validation] = {}
    for c in checks if isinstance(checks, list) else []:
        if not isinstance(c, Mapping):
            continue
        tags = c.get("tags")
        passed = c.get("passed") is True
        for tag in tags if isinstance(tags, list) else []:
            if not isinstance(tag, str):
                continue
            if not passed:
                out[tag] = Validation.FAILED
            elif out.get(tag) is not Validation.FAILED:
                out[tag] = Validation.PASSED
    return out


def _confidence_by_tag(confidence: object) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(confidence, Mapping):
        for tag, body in confidence.items():
            if isinstance(tag, str) and isinstance(body, Mapping):
                band = body.get("band")
                if isinstance(band, str):
                    out[tag] = band
    return out


def _results(doc_id: str, basis: str, checks: object) -> list[ValidationResult]:
    out: list[ValidationResult] = []
    seen: set[str] = set()
    for c in checks if isinstance(checks, list) else []:
        if not isinstance(c, Mapping):
            continue
        rule_id = c.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id or rule_id in seen:
            continue
        seen.add(rule_id)
        tags = c.get("tags")
        out.append(ValidationResult(
            doc_id=doc_id, basis=basis, rule_id=rule_id,
            kind=str(c.get("kind", "")), severity=str(c.get("severity", "")),
            passed=c.get("passed") is True,
            tags=tuple(sorted(t for t in tags if isinstance(t, str)))
            if isinstance(tags, list) else (),
            expected=str(c.get("expected", "")),
            actual=_decimal(c.get("actual")), delta=_decimal(c.get("delta")),
            tolerance=_decimal(c.get("tolerance")),
        ))
    return out


@dataclass
class _Basis:
    """One basis's working state."""

    report: ReportIdentity
    basis: str
    concepts: Mapping[str, Concept]
    validation: Mapping[str, Validation]
    confidence: Mapping[str, str]
    out: list[Observation] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def years(self, column: Column) -> str | None:
        fy = self.report.fiscal_year
        return fy if column is Column.CURRENT else previous_fiscal_year(fy)

    def observe(
        self, *, concept: Concept, column: Column, source: Source, value: Decimal | None,
        printed: str | None, unit: ReportingUnit, period_end: date | None, label: str,
        line_item: str | None = None, page: int | None = None, table_index: int | None = None,
        inputs: tuple[str, ...] = (), extracted_value_id: str | None = None,
    ) -> None:
        year = self.years(column)
        if year is None:
            self.count("comparative_year_unknown")
            return
        r = self.report
        self.out.append(Observation(
            doc_id=r.doc_id, doc_key=r.doc_key, version=r.version,
            report_fiscal_year=r.fiscal_year, company_id=r.company_id, basis=self.basis,
            concept=concept.tag, fiscal_year=year, column=column, source=source,
            value=value, printed=printed, unit=unit, period_kind=concept.period_kind,
            period_end=period_end, label=label, line_item=line_item, page=page,
            table_index=table_index,
            evidence_id=fact_ref(r.doc_id, self.basis, concept.tag).id,
            validation=self.validation.get(concept.tag, Validation.UNCHECKED),
            confidence=self.confidence.get(concept.tag), inputs=inputs,
            extracted_value_id=extracted_value_id,
        ))


def _period_ends(
    report: ReportIdentity, table: Mapping[str, object] | None
) -> tuple[date | None, date | None]:
    """(current, comparative) period ends: the column header's dates when it
    prints them, else the Report's own period end (S2) and a year before."""
    dates = _header_dates(table)
    current = dates[0] if dates else report.period_end
    comparative = dates[1] if len(dates) > 1 else previous_period_end(current)
    return current, comparative


def _printed_value(
    texts: Sequence[str], position: int, graph_value: object
) -> tuple[Decimal | None, str | None]:
    """(exact value, printed text) for one column of a row. The text is kept
    only when it parses back to the graph's own value; otherwise the graph's
    value stands, as a decimal, with no printed text -- never a mismatch."""
    graph = _decimal(graph_value)
    if graph is None:
        return None, None
    if position < len(texts):
        text = texts[position]
        if parse_amount(text) == float(graph_value):  # type: ignore[arg-type]
            exact = parse_decimal(text)
            if exact is not None:
                return exact, text
    return graph, None


def _nil_text(table: Mapping[str, object] | None, source_row: object) -> str:
    if table is None or not isinstance(source_row, int):
        return "-"
    cols = table_rows(table).get(source_row) or {}
    for c in sorted(cols):
        if c != 0 and is_nil_marker(cols[c]):
            return cols[c].strip()
    return "-"


def _graph_observations(
    st: _Basis, fog: Mapping[str, object], mapping: Mapping[str, object],
    ocr: Mapping[str, object],
) -> dict[Column, dict[str, Observation]]:
    """The arbitrated winners and printed nils, by column then Concept."""
    items: dict[str, Mapping[str, object]] = {}
    raw_items = _region(fog, st.basis).get("line_items")
    for item in raw_items if isinstance(raw_items, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("id"), str):
            items[str(item["id"])] = item

    units: dict[int, ReportingUnit] = {}

    def unit_of(item: Mapping[str, object]) -> ReportingUnit:
        index = item.get("table_index")
        if not isinstance(index, int) or isinstance(index, bool):
            return UNKNOWN
        if index not in units:
            units[index] = table_unit(ocr, index)
        return unit_for_kind(str(item.get("kind", "unknown")), units[index])

    claims = fact_claims(fog, mapping, basis=st.basis)
    for tag, claim in sorted(claims.items()):
        concept = st.concepts.get(tag)
        if concept is None:
            st.count("unknown_concept")
            continue
        item = items.get(claim.line_item)
        if item is None:
            continue
        table = _table(ocr, item.get("table_index"))
        origin = str(item.get("origin", ""))
        source = Source.GRAPH_FACE if origin == "face" else Source.GRAPH_NOTE
        source_row = item.get("source_row")
        texts = (printed_amounts(table, source_row, origin=origin)
                 if table is not None and isinstance(source_row, int) else ())
        unit = unit_of(item)
        if not unit.known:
            st.count("unit_unknown")
        current_end, comparative_end = _period_ends(st.report, table)
        page = item.get("page")
        index = item.get("table_index")
        label = str(item.get("label", ""))
        for column, position, raw, end in (
            (Column.CURRENT, 0, item.get("current"), current_end),
            (Column.COMPARATIVE, 1, item.get("prior"), comparative_end),
        ):
            if raw is None:
                continue
            value, printed = _printed_value(texts, position, raw)
            if value is None:
                continue
            if printed is None:
                st.count("printed_unverified")
            st.observe(
                concept=concept, column=column, source=source, value=value, printed=printed,
                unit=unit, period_end=end, label=label, line_item=claim.line_item,
                page=_int(page), table_index=_int(index),
            )

    # Printed nils on mapped lines no valued line claims.
    edges = _region(mapping, st.basis).get("edges")
    nil_seen: set[str] = set()
    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, Mapping) or edge.get("type") != "MAPS_TO":
            continue
        source_id, target = edge.get("source"), edge.get("target")
        if not isinstance(source_id, str) or not isinstance(target, str) or target in claims:
            continue
        if target in nil_seen or target not in st.concepts:
            continue
        item = items.get(source_id)
        if item is None or item.get("current") is not None:
            continue
        nil_seen.add(target)
        table = _table(ocr, item.get("table_index"))
        current_end, _ = _period_ends(st.report, table)
        st.observe(
            concept=st.concepts[target], column=Column.CURRENT, source=(
                Source.GRAPH_FACE if item.get("origin") == "face" else Source.GRAPH_NOTE),
            value=None, printed=_nil_text(table, item.get("source_row")),
            unit=unit_of(item), period_end=current_end, label=str(item.get("label", "")),
            line_item=source_id, page=_int(item.get("page")),
            table_index=_int(item.get("table_index")),
        )
        st.count("printed_nil")

    by_column: dict[Column, dict[str, Observation]] = {c: {} for c in Column}
    for o in st.out:
        if o.value is not None:
            by_column[o.column][o.concept] = o
    return by_column


def _compose(
    st: _Basis, printed: Mapping[str, Observation], formulas: Mapping[str, Formula],
    column: Column,
) -> None:
    """Every subtotal whose inputs are ALL stated, in exact decimals.

    Mirrors `fdi.core.compose`'s complete sums: a printed parent keeps its
    printed value inside the chain (REPORTED WINS), and its own children's sum
    is emitted beside it as a `graph_composed` Observation. A partial sum is
    never emitted -- it would be a number the inputs do not support. Inputs in
    different or unknown units are never added together."""
    known: dict[str, Observation] = dict(printed)
    composed: dict[str, tuple[Decimal, ReportingUnit, tuple[str, ...], date | None]] = {}

    def value_of(tag: str) -> tuple[Decimal, ReportingUnit, date | None] | None:
        if tag in known:
            o = known[tag]
            return (o.value, o.unit, o.period_end) if o.value is not None else None
        if tag in composed:
            v, u, _, end = composed[tag]
            return v, u, end
        return None

    def attempt(formula: Formula) -> tuple[Decimal, ReportingUnit, date | None] | None:
        total = Decimal(0)
        unit: ReportingUnit | None = None
        end: date | None = None
        for tag, weight in formula.inputs:
            got = value_of(tag)
            if got is None:
                return None
            v, u, e = got
            if not u.known or (unit is not None and u != unit):
                return None
            unit = u
            end = end or e
            total += v * Decimal(repr(weight))
        return (total, unit, end) if unit is not None else None

    sums = {t: f for t, f in formulas.items() if f.kind != "reconciliation"}
    for _ in range(_MAX_COMPOSE_PASSES):
        changed = False
        for tag, formula in sorted(sums.items()):
            if tag in known or tag in composed:
                continue
            got = attempt(formula)
            if got is not None:
                composed[tag] = (got[0], got[1], tuple(t for t, _ in formula.inputs), got[2])
                changed = True
        if not changed:
            break

    # Beside a printed total: its children's sum, where every child is stated.
    beside: dict[str, tuple[Decimal, ReportingUnit, tuple[str, ...], date | None]] = {}
    for tag, formula in sorted(sums.items()):
        if tag in known:
            got = attempt(formula)
            if got is not None:
                beside[tag] = (got[0], got[1], tuple(t for t, _ in formula.inputs), got[2])

    for tag, (value, unit, inputs, end) in sorted({**composed, **beside}.items()):
        concept = st.concepts.get(tag)
        if concept is None:
            continue
        st.observe(
            concept=concept, column=column, source=Source.GRAPH_COMPOSED, value=value,
            printed=None, unit=unit, period_end=end, label=concept.name, inputs=inputs,
        )
        st.count("composed")


def _statement_unit(observations: Sequence[Observation]) -> ReportingUnit:
    """The unit most of a basis's printed currency lines are in -- what an
    Extracted value read "in the document's own reporting unit" is in."""
    counts: dict[ReportingUnit, int] = {}
    for o in observations:
        # Per-share lines are whole rupees whatever the statement's unit is,
        # so they never vote.
        if (o.source is Source.GRAPH_FACE and o.unit.known and o.unit.currency
                and o.unit.scale != 1):
            counts[o.unit] = counts.get(o.unit, 0) + 1
    if not counts:
        return UNKNOWN
    return max(sorted(counts, key=lambda u: u.label), key=lambda u: counts[u])


def _extracted(
    st: _Basis, values: Sequence[ExtractedValue], end: date | None,
) -> None:
    unit = _statement_unit(st.out)
    for ev in values:
        concept = st.concepts.get(ev.tag)
        if concept is None:
            st.count("extraction_unknown_concept")
            continue
        source = (Source.EXTRACTION_CORRECTED if ev.action == "corrected"
                  else Source.EXTRACTION_ACCEPTED)
        pairs: list[tuple[Column, object]]
        if isinstance(ev.value, Mapping):
            pairs = [(Column.CURRENT, ev.value.get("current")),
                     (Column.COMPARATIVE, ev.value.get("prior"))]
        else:
            pairs = [(Column.CURRENT, ev.value)]
        for column, raw in pairs:
            value = _decimal(raw)
            if value is None:
                continue
            kind_unit = unit_for_kind(classify_quantity(concept.name, None), unit)
            st.observe(
                concept=concept, column=column, source=source, value=value, printed=None,
                unit=kind_unit,
                period_end=end if column is Column.CURRENT else previous_period_end(end),
                label=ev.label or concept.name, extracted_value_id=ev.id,
            )
            st.count("extraction")


def project_report(
    report: ReportIdentity,
    *,
    fog: Mapping[str, object],
    mapping: Mapping[str, object],
    reasoning: Mapping[str, object],
    ocr: Mapping[str, object],
    concepts: Mapping[str, Concept],
    formulas: Mapping[str, Formula],
    extracted: Sequence[ExtractedValue] = (),
) -> Projection:
    """Every Observation and validation result one Report contributes, in a
    stable order. Total: a missing or malformed artifact yields fewer rows,
    never an exception."""
    observations: list[Observation] = []
    validations: list[ValidationResult] = []
    diagnostics: dict[str, object] = {"projection_version": PROJECTION_VERSION}
    bases = sorted(set(_bases(fog)) | {v.basis for v in extracted if v.basis})
    for basis in bases:
        region = _region(reasoning, basis)
        st = _Basis(
            report=report, basis=basis, concepts=concepts,
            validation=_validation_by_tag(region.get("checks")),
            confidence=_confidence_by_tag(region.get("confidence")),
        )
        printed = _graph_observations(st, fog, mapping, ocr)
        for column in Column:
            _compose(st, printed[column], formulas, column)
        _extracted(st, [v for v in extracted if v.basis == basis], report.period_end)
        observations.extend(st.out)
        validations.extend(_results(report.doc_id, basis, region.get("checks")))
        coverage = region.get("coverage")
        not_answered = coverage.get("not_answered") if isinstance(coverage, Mapping) else None
        diagnostics[basis] = {
            **dict(sorted(st.counts.items())),
            "observations": len(st.out),
            "not_answered": sorted(t for t in not_answered if isinstance(t, str))
            if isinstance(not_answered, list) else [],
        }
    skipped = [v.id for v in extracted if not v.basis]
    if skipped:
        diagnostics["extraction_basis_unknown"] = len(skipped)

    # One row per identity: a later duplicate (it cannot happen from one
    # graph, but an Extracted value may repeat) never silently replaces the
    # first.
    unique: dict[tuple[str, ...], Observation] = {}
    for o in observations:
        unique.setdefault(o.identity, o)
    ordered = sorted(unique.values(), key=lambda o: o.identity)
    return Projection(
        observations=tuple(ordered),
        validations=tuple(sorted(validations, key=lambda v: (v.basis, v.rule_id))),
        diagnostics=diagnostics,
    )


def line_item_evidence_id(o: Observation) -> str | None:
    """The `line_item` Evidence id (S4) of the line an Observation was read
    from, or None for a composed or extracted one."""
    if o.line_item is None:
        return None
    ref = ref_for_fog_node(o.doc_id, o.line_item)
    return ref.id if ref is not None else None
