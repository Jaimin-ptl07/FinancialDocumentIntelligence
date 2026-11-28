"""The Financial fact service (S5, ADR-0006): one interface over Financial
facts, their Observations and the Calculation engine.

Two jobs:

* **project a Report** -- read its graph, reasoning and OCR artifacts and its
  Invitees' reviewed Extracted values, turn them into Observations and
  validation results (`fdi.core.fact_projection`, pure), store them, and
  re-derive its Company's Financial facts (`fdi.core.financial_facts`, pure).
  Called when the reasoning stage completes, after a Report's Company or
  Fiscal year changes, and by `fdi-facts backfill`. Projecting unchanged
  inputs again is a no-op, and projecting a Report twice never duplicates a
  row: Observation ids are derived from their identity.
* **answer** -- facts with their Observations, validation results and gaps
  for Company x Concepts x Fiscal years x basis; the history of a Company; a
  Metric; and comparisons, through the deterministic Calculation engine.

Only the projection writes these tables. A Company's facts are re-derived in
the same transaction that changes its Observations, under a row lock on the
Company, so two Reports of one Company projected at once cannot interleave.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, cast

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdi.core.calculation import (
    CATALOGUE,
    Metric,
    MetricResult,
    Verdict,
    calculate,
    comparability,
)
from fdi.core.compose import read_formulas
from fdi.core.fact_projection import (
    PROJECTION_VERSION,
    ExtractedValue,
    Projection,
    ReportIdentity,
    ValidationResult,
    project_report,
)
from fdi.core.financial_facts import (
    Column,
    Concept,
    FinancialFact,
    GapReason,
    Observation,
    PeriodKind,
    Source,
    Validation,
    concepts_from_schema,
    gap_reason,
    previous_fiscal_year,
    select_facts,
)
from fdi.core.hashing import sha256_hex
from fdi.core.reporting_unit import ReportingUnit
from fdi.core.serialization import canonical_json_bytes
from fdi.pipeline.crossdoc import read_artifact
from fdi.platform.db.models import (
    Company,
    ConceptRow,
    Document,
    ExtractionField,
    FieldRead,
    FinancialFactRow,
    FinancialObservationRow,
    FinancialProjection,
    FinancialValidationRow,
    ValueReview,
)
from fdi.platform.db.repository import CompanyRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore

log = structlog.get_logger(__name__)

# The artifacts a projection reads. Their output hashes are part of the
# projection key, so a re-run stage re-projects and nothing else does.
PROJECTION_STAGES: Final = ("fog_objects", "fog_mapping", "fog_reason", "ocr")
_ID_NAMESPACE: Final = uuid.UUID("5d0e6f43-6a3b-4c0f-9a1e-3f0b9c8e5a21")


def _stable_id(*parts: object) -> str:
    return str(uuid.uuid5(_ID_NAMESPACE, "|".join(str(p) for p in parts)))


def observation_id(o: Observation) -> str:
    return _stable_id("obs", *o.identity)


def next_fiscal_year(label: str) -> str | None:
    """The Fiscal year after `label` (FY2024-25 -> FY2025-26), the inverse of
    `previous_fiscal_year`. None for an unrecognised label."""
    if label.startswith("FY") and len(label) == 9 and label[6] == "-":
        try:
            start = int(label[2:6]) + 1
        except ValueError:
            return None
        return f"FY{start}-{(start + 1) % 100:02d}"
    if label.startswith("FY") and len(label) == 6 and label[2:].isdigit():
        return f"FY{int(label[2:]) + 1}"
    return None


# -- row <-> core -------------------------------------------------------------


def _to_row(o: Observation) -> FinancialObservationRow:
    return FinancialObservationRow(
        id=observation_id(o), doc_id=o.doc_id, company_id=o.company_id, basis=o.basis,
        concept=o.concept, fiscal_year=o.fiscal_year, report_column=o.column.value,
        source=o.source.value, doc_key=o.doc_key, version=o.version,
        report_fiscal_year=o.report_fiscal_year, value=o.value, printed=o.printed,
        currency=o.unit.currency, scale=o.unit.scale, unit=o.unit.label,
        unit_printed=o.unit.printed, normalised=o.normalised,
        period_kind=o.period_kind.value, period_end=o.period_end, label=o.label,
        line_item=o.line_item, page=o.page, table_index=o.table_index,
        evidence_id=o.evidence_id, validation=o.validation.value, confidence=o.confidence,
        inputs=list(o.inputs), extracted_value_id=o.extracted_value_id,
    )


def _from_row(r: FinancialObservationRow) -> Observation:
    return Observation(
        doc_id=r.doc_id, doc_key=r.doc_key, version=r.version,
        report_fiscal_year=r.report_fiscal_year, company_id=r.company_id, basis=r.basis,
        concept=r.concept, fiscal_year=r.fiscal_year, column=Column(r.report_column),
        source=Source(r.source),
        # NUMERIC comes back as Decimal with the stored exponent: "2681.75"
        # stays two places, which is what rounding tolerance reads.
        value=r.value, printed=r.printed,
        unit=ReportingUnit(r.currency, Decimal(r.scale), r.unit, r.unit_printed),
        period_kind=PeriodKind(r.period_kind), period_end=r.period_end, label=r.label,
        line_item=r.line_item, page=r.page, table_index=r.table_index,
        evidence_id=r.evidence_id, validation=Validation(r.validation),
        confidence=r.confidence, inputs=tuple(r.inputs or ()),
        extracted_value_id=r.extracted_value_id,
    )


def _validation_row(v: ValidationResult) -> FinancialValidationRow:
    return FinancialValidationRow(
        id=_stable_id("val", v.doc_id, v.basis, v.rule_id), doc_id=v.doc_id,
        basis=v.basis, rule_id=v.rule_id, kind=v.kind, severity=v.severity,
        passed=v.passed, tags=list(v.tags), expected=v.expected, actual=v.actual,
        delta=v.delta, tolerance=v.tolerance,
    )


def _fact_row(f: FinancialFact) -> FinancialFactRow:
    return FinancialFactRow(
        id=_stable_id("fact", *f.key), company_id=f.company_id, concept=f.concept,
        fiscal_year=f.fiscal_year, basis=f.basis, status=f.status.value,
        selected_observation_id=observation_id(f.selected), value=f.value,
        normalised=f.normalised, unit=f.unit.label, restated=f.restated,
        restatement_original_id=(
            observation_id(f.restatement.original) if f.restatement else None),
        disagreement_original_id=(
            observation_id(f.disagreement.original) if f.disagreement else None),
        derived_observation_id=observation_id(f.derived) if f.derived else None,
        observation_ids=[observation_id(o) for o in f.observations],
    )


# -- results ------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectionOutcome:
    doc_id: str
    status: str                 # "projected" | "unchanged" | "skipped"
    reason: str = ""
    observations: int = 0
    companies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Gap:
    concept: str
    fiscal_year: str
    basis: str
    reason: GapReason


@dataclass(frozen=True)
class FactsResult:
    company_id: str
    basis: str
    facts: tuple[FinancialFact, ...]
    gaps: tuple[Gap, ...]
    validations: Mapping[str, tuple[FinancialValidationRow, ...]] = field(
        default_factory=dict)   # keyed by observation id


class FinancialFactsError(ValueError):
    """A request the service cannot serve (unknown Metric, unknown Company)."""


@dataclass
class _Corpus:
    """One Company x basis worth of Observations, read once."""

    observations: list[Observation]
    facts: list[FinancialFact]

    def by_key(self) -> dict[tuple[str, str], FinancialFact]:
        return {(f.concept, f.fiscal_year): f for f in self.facts}


class FinancialFactService:
    """Build one per unit of work: it shares the caller's session.

    `formulas_schema` is the taxonomy document the graph stages reason with
    (`load_schema`); its derivations decide what the projection composes."""

    def __init__(
        self, *, session: AsyncSession, store: ArtifactStore,
        formulas_schema: Mapping[str, object] | None = None,
    ) -> None:
        self._s = session
        self._store = store
        self._formulas_schema: Mapping[str, object] = formulas_schema or {}
        self._formulas = read_formulas(self._formulas_schema)

    # -- Concepts ---------------------------------------------------------------

    async def seed_concepts(self, schema: Mapping[str, object]) -> int:
        """Insert every Concept of a standard schema document that is not yet
        stored. Existing Concepts are left alone: a tag's meaning is never
        rewritten under facts that use it. Returns how many were added."""
        concepts = concepts_from_schema(schema)
        version = str(schema.get("version", "")) if isinstance(schema, Mapping) else ""
        existing = set((await self._s.execute(sa.select(ConceptRow.tag))).scalars())
        added = [
            ConceptRow(tag=c.tag, name=c.name, statement=c.statement,
                       period_kind=c.period_kind.value, source_version=version)
            for tag, c in sorted(concepts.items()) if tag not in existing
        ]
        self._s.add_all(added)
        await self._s.flush()
        return len(added)

    async def concepts(self) -> dict[str, Concept]:
        rows = (await self._s.execute(sa.select(ConceptRow))).scalars()
        return {
            r.tag: Concept(r.tag, r.name, r.statement, PeriodKind(r.period_kind)) for r in rows
        }

    # -- projection ---------------------------------------------------------------

    async def project(self, doc_id: str, *, force: bool = False) -> ProjectionOutcome:
        """Project one Report version and re-derive its Company's facts, in one
        transaction committed here. Never raises on a missing artifact: an
        unprocessed or unlinked Report is recorded as `skipped` with why."""
        doc = await self._s.get(Document, doc_id)
        if doc is None:
            raise FinancialFactsError(f"no such document {doc_id}")
        previous = await self._s.get(FinancialProjection, doc_id)
        old_company = previous.company_id if previous is not None else None

        skip_reason = ""
        if doc.company_id is None:
            skip_reason = "not filed under a Company"
        elif not doc.fiscal_year:
            skip_reason = "no Fiscal year"
        registry = RegistryRepository(self._s)
        runs = {s: await registry.latest_completed_run(doc_id, s) for s in PROJECTION_STAGES}
        if not skip_reason and any(r is None or r.artifact_key is None for r in runs.values()):
            missing = sorted(s for s, r in runs.items() if r is None or r.artifact_key is None)
            skip_reason = "not processed: " + ", ".join(missing)
        concepts = await self.concepts()
        if not skip_reason and not concepts:
            skip_reason = "no Concepts seeded"

        extracted = await self._extracted_values(doc_id) if not skip_reason else []
        key = sha256_hex(canonical_json_bytes({
            "v": PROJECTION_VERSION,
            "runs": {s: (r.output_hash if r is not None else None) for s, r in runs.items()},
            "doc": [doc.company_id, doc.fiscal_year, doc.doc_key, doc.version,
                    doc.period_end.isoformat() if doc.period_end else None],
            "concepts": sorted(concepts),
            "formulas": sha256_hex(canonical_json_bytes(self._formulas_schema)),
            "extracted": [[e.id, e.tag, e.action, e.basis, e.value] for e in extracted],
            "skip": skip_reason,
        }))
        if (not force and previous is not None and previous.projection_key == key):
            return ProjectionOutcome(doc_id, "unchanged", previous.reason,
                                     previous.observation_count)

        projection: Projection | None = None
        if not skip_reason:
            artifacts: dict[str, Mapping[str, object]] = {}
            for stage in PROJECTION_STAGES:
                body = await read_artifact(registry, self._store, doc_id, stage)
                if body is None:
                    skip_reason = f"artifact unreadable: {stage}"
                    break
                artifacts[stage] = body
            if not skip_reason:
                identity = ReportIdentity(
                    doc_id=doc.id, doc_key=doc.doc_key, version=doc.version,
                    company_id=cast(str, doc.company_id),
                    fiscal_year=cast(str, doc.fiscal_year), period_end=doc.period_end,
                )
                # CPU-bound over a multi-megabyte OCR body: off the event loop.
                projection = await asyncio.to_thread(
                    project_report, identity, fog=artifacts["fog_objects"],
                    mapping=artifacts["fog_mapping"], reasoning=artifacts["fog_reason"],
                    ocr=artifacts["ocr"], concepts=concepts, formulas=self._formulas,
                    extracted=extracted,
                )

        companies = sorted({c for c in (old_company, doc.company_id) if c})
        await self._lock_companies(companies)
        await self._s.execute(
            sa.delete(FinancialObservationRow).where(FinancialObservationRow.doc_id == doc_id))
        await self._s.execute(
            sa.delete(FinancialValidationRow).where(FinancialValidationRow.doc_id == doc_id))
        count = 0
        if projection is not None:
            self._s.add_all(_to_row(o) for o in projection.observations)
            self._s.add_all(_validation_row(v) for v in projection.validations)
            count = len(projection.observations)
        await self._s.flush()
        status = "skipped" if projection is None else "projected"
        values: dict[str, object] = {
            "company_id": doc.company_id, "fiscal_year": doc.fiscal_year,
            "projection_key": key, "status": status, "reason": skip_reason,
            "observation_count": count,
            "diagnostics": dict(projection.diagnostics) if projection is not None else {},
            "projected_at": sa.func.now(),
        }
        if previous is None:
            self._s.add(FinancialProjection(doc_id=doc_id, **values))
        else:
            for name, value in values.items():
                setattr(previous, name, value)
        await self._s.flush()
        for company_id in companies:
            await self._rebuild_facts(company_id)
        await self._s.commit()
        log.info("facts.projected", doc_id=doc_id, status=status, reason=skip_reason,
                 observations=count, companies=companies)
        return ProjectionOutcome(doc_id, status, skip_reason, count, tuple(companies))

    async def rebuild_company(self, company_id: str) -> int:
        """Re-derive one Company's facts from its stored Observations (after a
        Report of it was deleted, say). Commits. Returns the fact count."""
        await self._lock_companies([company_id])
        n = await self._rebuild_facts(company_id)
        await self._s.commit()
        return n

    async def _lock_companies(self, company_ids: Sequence[str]) -> None:
        """Serialise projections that touch the same Company: re-deriving its
        facts reads every Observation it has, so two must not interleave.
        Locked in id order, so two transactions never deadlock."""
        for company_id in sorted(company_ids):
            await self._s.execute(
                sa.select(Company.id).where(Company.id == company_id).with_for_update())

    async def _rebuild_facts(self, company_id: str) -> int:
        rows = (await self._s.execute(
            sa.select(FinancialObservationRow)
            .where(FinancialObservationRow.company_id == company_id)
        )).scalars().all()
        facts = select_facts(_from_row(r) for r in rows)
        await self._s.execute(
            sa.delete(FinancialFactRow).where(FinancialFactRow.company_id == company_id))
        self._s.add_all(_fact_row(f) for f in facts)
        await self._s.flush()
        return len(facts)

    async def _extracted_values(self, doc_id: str) -> list[ExtractedValue]:
        """Each Field's latest review on this Report version, where the Field
        names a Concept. A correction carries its own value; an acceptance
        carries the read's."""
        q = (
            sa.select(FieldRead.id, FieldRead.field_id, FieldRead.basis, FieldRead.value,
                      ValueReview.action, ValueReview.value)
            .join(ValueReview, ValueReview.read_id == FieldRead.id)
            .where(FieldRead.doc_id == doc_id)
            .order_by(FieldRead.field_id, ValueReview.created_at.desc(), ValueReview.id.desc())
        )
        latest: dict[str, tuple[str, str | None, object, str, object]] = {}
        for read_id, field_id, basis, read_value, action, review_value in (
            await self._s.execute(q)
        ).all():
            latest.setdefault(field_id, (read_id, basis, read_value, action, review_value))
        if not latest:
            return []
        tags: dict[str, tuple[str, str]] = {}
        for field_id, tag, name in (await self._s.execute(
            sa.select(ExtractionField.field_id, ExtractionField.tag, ExtractionField.name)
            .where(ExtractionField.field_id.in_(sorted(latest)),
                   ExtractionField.tag.is_not(None))
            .order_by(ExtractionField.field_id, ExtractionField.id)
        )).all():
            tags.setdefault(field_id, (cast(str, tag), name))
        out: list[ExtractedValue] = []
        for field_id, (read_id, basis, read_value, action, review_value) in sorted(
            latest.items()
        ):
            if field_id not in tags:
                continue
            tag, name = tags[field_id]
            out.append(ExtractedValue(
                id=read_id, tag=tag, action=action,
                value=review_value if action == "corrected" else read_value,
                basis=basis, label=name,
            ))
        return out

    # -- answering ----------------------------------------------------------------

    async def _corpus(
        self, company_id: str, basis: str, concepts: Iterable[str] | None = None,
        fiscal_years: Iterable[str] | None = None,
    ) -> _Corpus:
        q = sa.select(FinancialObservationRow).where(
            FinancialObservationRow.company_id == company_id,
            FinancialObservationRow.basis == basis,
        )
        if concepts is not None:
            q = q.where(FinancialObservationRow.concept.in_(sorted(set(concepts))))
        if fiscal_years is not None:
            q = q.where(FinancialObservationRow.fiscal_year.in_(sorted(set(fiscal_years))))
        observations = [_from_row(r) for r in (await self._s.execute(q)).scalars()]
        return _Corpus(observations, select_facts(observations))

    async def facts(
        self, *, company_id: str, concepts: Sequence[str], fiscal_years: Sequence[str],
        basis: str,
    ) -> FactsResult:
        """The facts for each requested Concept x Fiscal year, with every
        Observation behind them, and a gap -- with its reason -- for each one
        FDI has no value for."""
        corpus = await self._corpus(company_id, basis, concepts, fiscal_years)
        found = corpus.by_key()
        facts = tuple(found[(c, fy)] for fy in fiscal_years for c in concepts if (c, fy) in found)
        missing = [(c, fy) for fy in fiscal_years for c in concepts if (c, fy) not in found]
        gaps = await self._gaps(company_id, basis, missing)
        validations = await self._validations(facts)
        return FactsResult(company_id, basis, facts, tuple(gaps), validations)

    async def history(
        self, *, company_id: str, concepts: Sequence[str], basis: str,
        fiscal_years: Sequence[str] | None = None,
    ) -> FactsResult:
        """A Company's multi-year table for `concepts`: every Fiscal year any
        of its Reports covers (or just `fiscal_years`), oldest first."""
        if fiscal_years is None:
            years = sorted({
                fy for (fy,) in (await self._s.execute(
                    sa.select(FinancialFactRow.fiscal_year).distinct().where(
                        FinancialFactRow.company_id == company_id,
                        FinancialFactRow.basis == basis,
                    ))).all()
            })
        else:
            years = sorted(set(fiscal_years))
        return await self.facts(
            company_id=company_id, concepts=concepts, fiscal_years=years, basis=basis)

    async def fact(
        self, *, company_id: str, concept: str, fiscal_year: str, basis: str,
    ) -> tuple[FinancialFact | None, Gap | None, tuple[FinancialValidationRow, ...]]:
        result = await self.facts(company_id=company_id, concepts=[concept],
                                  fiscal_years=[fiscal_year], basis=basis)
        if result.facts:
            f = result.facts[0]
            return f, None, result.validations.get(observation_id(f.selected), ())
        return None, (result.gaps[0] if result.gaps else None), ()

    async def _validations(
        self, facts: Sequence[FinancialFact],
    ) -> dict[str, tuple[FinancialValidationRow, ...]]:
        """The checks that involve each fact's selected Observation, on its own
        Report version and basis."""
        wanted = {(f.selected.doc_id, f.selected.basis) for f in facts}
        if not wanted:
            return {}
        rows = (await self._s.execute(
            sa.select(FinancialValidationRow).where(
                FinancialValidationRow.doc_id.in_(sorted({d for d, _ in wanted})))
        )).scalars().all()
        by_doc: dict[tuple[str, str], list[FinancialValidationRow]] = {}
        for r in rows:
            by_doc.setdefault((r.doc_id, r.basis), []).append(r)
        out: dict[str, tuple[FinancialValidationRow, ...]] = {}
        for f in facts:
            s = f.selected
            out[observation_id(s)] = tuple(
                r for r in by_doc.get((s.doc_id, s.basis), []) if s.concept in (r.tags or [])
            )
        return out

    async def _gaps(
        self, company_id: str, basis: str, missing: Sequence[tuple[str, str]],
    ) -> list[Gap]:
        if not missing:
            return []
        docs = await CompanyRepository(self._s).latest_documents(company_id)
        projections = {
            p.doc_id: p for p in (await self._s.execute(
                sa.select(FinancialProjection).where(
                    FinancialProjection.doc_id.in_([d.id for d in docs])))
            ).scalars()
        } if docs else {}
        out: list[Gap] = []
        for concept, fy in missing:
            # A Fiscal year is printed by its own Report and, as the
            # comparative column, by the next one.
            covering = [d for d in docs if d.fiscal_year in {fy, next_fiscal_year(fy)}]
            processed = [p for d in covering if (p := projections.get(d.id)) is not None
                         and p.status == "projected"]
            missed = any(
                concept in cast(Mapping[str, list[str]], (p.diagnostics or {}).get(basis) or {})
                .get("not_answered", [])
                for p in processed
            )
            out.append(Gap(concept, fy, basis, gap_reason(
                report_exists=bool(covering), processed=bool(processed), missed=missed)))
        return out

    async def observation_for_evidence(
        self, doc_id: str, basis: str, concept: str,
    ) -> Observation | None:
        """The current-column Observation a `{doc}:fact:{basis}:{tag}` Evidence
        id resolves to -- printed first, then composed (S4's structured path)."""
        rows = (await self._s.execute(
            sa.select(FinancialObservationRow).where(
                FinancialObservationRow.doc_id == doc_id,
                FinancialObservationRow.basis == basis,
                FinancialObservationRow.concept == concept,
                FinancialObservationRow.report_column == Column.CURRENT.value,
                FinancialObservationRow.source.in_(
                    [Source.GRAPH_FACE.value, Source.GRAPH_NOTE.value,
                     Source.GRAPH_COMPOSED.value]),
            )
        )).scalars().all()
        ranked = sorted(
            (_from_row(r) for r in rows),
            key=lambda o: (o.source is not Source.GRAPH_FACE,
                           o.source is not Source.GRAPH_NOTE, o.source.value),
        )
        return ranked[0] if ranked else None

    async def report_values(self, doc_id: str) -> dict[str, dict[str, dict[str, str | None]]]:
        """basis -> tag -> {current, prior}: the values one Report version
        prints, as the fact service stores them (printed Observations only,
        exact decimals as strings). What `score_pipeline.py --facts` scores,
        so the service's score can be set against the artifact's."""
        rows = (await self._s.execute(
            sa.select(FinancialObservationRow).where(
                FinancialObservationRow.doc_id == doc_id,
                FinancialObservationRow.source.in_(
                    [Source.GRAPH_FACE.value, Source.GRAPH_NOTE.value]),
                FinancialObservationRow.value.is_not(None),
            ).order_by(FinancialObservationRow.basis, FinancialObservationRow.concept)
        )).scalars().all()
        out: dict[str, dict[str, dict[str, str | None]]] = {}
        for r in rows:
            cell = out.setdefault(r.basis, {}).setdefault(
                r.concept, {"current": None, "prior": None})
            key = "current" if r.report_column == Column.CURRENT.value else "prior"
            cell[key] = format(r.value, "f") if r.value is not None else None
        return {b: {t: v for t, v in tags.items() if v["current"] is not None}
                for b, tags in out.items()}

    async def bases(self, doc_id: str) -> list[str]:
        """The bases a projected Report version has Observations on."""
        rows = await self._s.execute(
            sa.select(FinancialObservationRow.basis).distinct()
            .where(FinancialObservationRow.doc_id == doc_id))
        return sorted(b for (b,) in rows.all())

    async def projected(self, doc_id: str) -> bool:
        p = await self._s.get(FinancialProjection, doc_id)
        return p is not None and p.status == "projected"

    # -- the Calculation engine ------------------------------------------------------

    @staticmethod
    def metric(name: str) -> Metric:
        metric = CATALOGUE.get(name)
        if metric is None:
            raise FinancialFactsError(f"unknown metric {name!r}")
        return metric

    async def _metric_facts(
        self, metric: Metric, company_id: str, basis: str, fiscal_years: Sequence[str],
    ) -> dict[tuple[str, str], FinancialFact]:
        concepts = {c for i in metric.inputs for c in i.concepts}
        years = set(fiscal_years)
        for fy in fiscal_years:
            y: str | None = fy
            for _ in range(max((-i.offset for i in metric.inputs), default=0)):
                y = previous_fiscal_year(y)
                if y is not None:
                    years.add(y)
        return (await self._corpus(company_id, basis, concepts, years)).by_key()

    async def calculate(
        self, *, metric: str, company_id: str, fiscal_years: Sequence[str], basis: str,
    ) -> list[MetricResult]:
        m = self.metric(metric)
        facts = await self._metric_facts(m, company_id, basis, fiscal_years)
        return calculate(m, company_id=company_id, fiscal_years=fiscal_years, basis=basis,
                         facts=facts)

    async def compare_periods(
        self, *, metric: str, company_id: str, fiscal_years: Sequence[str], basis: str,
    ) -> tuple[list[MetricResult], list[Verdict]]:
        """A Metric across a Company's Fiscal years, oldest first, with the
        comparability verdict for each consecutive pair."""
        years = sorted(set(fiscal_years))
        results = await self.calculate(metric=metric, company_id=company_id,
                                       fiscal_years=years, basis=basis)
        verdicts = [comparability(a, b) for a, b in zip(results, results[1:], strict=False)]
        return results, verdicts

    async def compare_companies(
        self, *, metric: str, company_ids: Sequence[str], fiscal_year: str, basis: str,
    ) -> tuple[list[MetricResult], list[Verdict]]:
        """One Metric for several Companies in one Fiscal year, with each
        Company's comparability verdict against the first."""
        results: list[MetricResult] = []
        for company_id in company_ids:
            results.extend(await self.calculate(
                metric=metric, company_id=company_id, fiscal_years=[fiscal_year], basis=basis))
        verdicts = [comparability(results[0], r, same_period=True) for r in results[1:]]
        return results, verdicts


def read_json_document(path: str) -> Mapping[str, object]:
    with open(path, encoding="utf-8") as fh:
        body = json.load(fh)
    return body if isinstance(body, Mapping) else {}


async def project_document(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    doc_id: str,
    formulas_schema: Mapping[str, object],
    standard_schema_path: str,
    force: bool = False,
) -> ProjectionOutcome:
    """Project one Report version in its own session, seeding the Concepts
    from the standard schema document first if none are stored yet. The one
    entry point the worker, the API and `fdi-facts` share."""
    async with session_factory() as session:
        service = FinancialFactService(
            session=session, store=store, formulas_schema=formulas_schema)
        if not await service.concepts():
            standard = await asyncio.to_thread(read_json_document, standard_schema_path)
            added = await service.seed_concepts(standard)
            await session.commit()
            log.info("facts.concepts_seeded", added=added, path=standard_schema_path)
        return await service.project(doc_id, force=force)
