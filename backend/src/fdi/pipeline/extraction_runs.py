"""Extraction runs, Extracted values and their review (S3).

`ExtractionService` answers the questions S3 promises and the S8 Extraction
agent will ask: which Fields of a schema version are missing or stale for
these Reports (`plan`), run them (`create_run` + the worker's `execute_run`),
what a run found (`run_view`, `results`), what is known for one Report
(`report_values`), and accept or correct a value (`review`).

Execution contract (`execute_run`), at-least-once like every job here:
* a Report already finished by this run is skipped on redelivery;
* reads are stored group by group by the engine, so a redelivered Report
  reuses everything the lost attempt paid for -- including its forced
  re-reads, which are not forced a second time;
* every planned Field x Report gets one `extraction_run_item` with the
  plan's reason and the outcome, idempotent on (run, Report, Field);
* a Report's failure is recorded on the Report and the run moves on; the
  run ends `succeeded`, `partial` (something failed) or `failed` (nothing
  could be read).

Values are judged when shown (`fdi.core.extracted_values`), never stored:
review status from the version's rules and threshold, derived Fields from
the effective (accepted/corrected) values of their inputs.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdi.core.extracted_values import compute_derived, validate_value, value_status
from fdi.core.extraction_plan import (
    PlannedField,
    StoredRead,
    plan_extraction,
)
from fdi.core.extraction_schema import (
    FieldDefinition,
    field_from_mapping,
    resolve_group,
    to_field_spec,
)
from fdi.core.extractor import build_extraction_record
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.extraction_schemas import SchemaService, VersionView
from fdi.pipeline.extractor import (
    EngineField,
    ExtractContext,
    ExtractorClient,
    engine_versions,
    extract_fields,
)
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.extraction_repository import ExtractionRepository
from fdi.platform.db.models import (
    ExtractionField,
    ExtractionRun,
    ExtractionSchemaVersion,
    FieldRead,
    ValueReview,
)
from fdi.platform.db.repository import DocumentRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore

log = structlog.get_logger(__name__)

TERMINAL: Final = frozenset({"succeeded", "partial", "failed"})
NOT_EXTRACTED: Final = "not_extracted"


class RunNotFound(LookupError):
    pass


class ReportNotFound(LookupError):
    pass


class ReadNotFound(LookupError):
    pass


class RunRejected(ValueError):
    """The run cannot be accepted as asked; the message says why."""


class NothingToRetry(RuntimeError):
    pass


# -- views -----------------------------------------------------------------


@dataclass(frozen=True)
class ReviewView:
    action: str
    value: object
    note: str
    user_id: str
    created_at: datetime


@dataclass(frozen=True)
class ValueView:
    """One Field's value in one Report, as an Invitee sees it."""

    field_id: str
    status: str
    value: object
    value_as_read: object
    confidence: float | None
    basis: str | None
    read_id: str | None
    provenance: Mapping[str, object]
    problems: tuple[str, ...]
    review: ReviewView | None
    derived_from: tuple[str, ...] = ()
    missing_inputs: tuple[str, ...] = ()
    error: str | None = None
    read_at: datetime | None = None
    stale: bool = False


@dataclass(frozen=True)
class FieldColumn:
    field_id: str
    name: str
    tag: str | None
    origin: str
    data_type: str
    unit: str | None


@dataclass(frozen=True)
class ReportRow:
    doc_id: str
    doc_key: str | None
    fiscal_year: str | None
    company_id: str | None
    values: tuple[ValueView, ...]


@dataclass(frozen=True)
class ResultTable:
    schema_id: str
    version: int
    columns: tuple[FieldColumn, ...]
    rows: tuple[ReportRow, ...]


@dataclass(frozen=True)
class RunReportView:
    doc_id: str
    status: str
    counts: Mapping[str, int]
    error: str | None


@dataclass(frozen=True)
class RunView:
    id: str
    schema_id: str
    version: int
    status: str
    field_ids: tuple[str, ...] | None
    forced_field_ids: tuple[str, ...]
    counts: Mapping[str, int]
    reports: tuple[RunReportView, ...]
    retry_of: str | None
    api_job_id: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class PlanCell:
    doc_id: str
    field_id: str
    action: str
    reason: str


@dataclass(frozen=True)
class PlanView:
    schema_id: str
    version: int
    cells: tuple[PlanCell, ...]
    summary: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineDeps:
    """What executing a run needs beyond the database."""

    store: ArtifactStore
    embedder: EmbeddingClient | None
    vector_store: VectorStore | None
    client: ExtractorClient


# -- helpers ---------------------------------------------------------------


def _engine_fields(view: VersionView, field_ids: Sequence[str]) -> list[EngineField]:
    groups = {g.key: g for g in view.groups}
    by_id = {f.field_id: f for f in view.fields}
    out: list[EngineField] = []
    for fid in field_ids:
        f = by_id[fid]
        if f.origin == "derived":
            continue
        out.append(EngineField(
            field_id=f.field_id, definition_hash=view.definition_hashes[f.field_id],
            spec=to_field_spec(f, resolve_group(f, groups)),
        ))
    return out


def _review_view(r: ValueReview | None) -> ReviewView | None:
    if r is None:
        return None
    return ReviewView(action=r.action, value=r.value, note=r.note, user_id=r.user_id,
                      created_at=r.created_at)


def _value_view(
    f: FieldDefinition,
    read: FieldRead | None,
    review: ValueReview | None,
    threshold: float,
    *,
    stale: bool = False,
) -> ValueView:
    if read is None:
        return ValueView(field_id=f.field_id, status=NOT_EXTRACTED, value=None,
                         value_as_read=None, confidence=None, basis=None, read_id=None,
                         provenance={}, problems=(), review=None)
    effective = review.value if review is not None and review.action == "corrected" else read.value
    problems = tuple(validate_value(read.value, f))
    status = value_status(
        read_status=read.status, review_action=review.action if review else None,
        problems=problems, confidence=read.confidence, threshold=threshold,
        required=f.required,
    )
    return ValueView(
        field_id=f.field_id, status=status, value=effective, value_as_read=read.value,
        confidence=read.confidence, basis=read.basis, read_id=read.id,
        provenance=dict(read.provenance or {}), problems=problems,
        review=_review_view(review), error=read.error, read_at=read.created_at, stale=stale,
    )


def _with_derived(
    fields: Sequence[FieldDefinition], values: Mapping[str, ValueView]
) -> dict[str, ValueView]:
    """`values` plus every derived Field, computed from the EFFECTIVE values
    of its inputs (`failed`/`not_extracted` inputs count as absent)."""
    by_tag: dict[str, object] = {}
    for f in fields:
        v = values.get(f.field_id)
        if f.tag and f.origin != "derived" and v is not None and v.status not in (
            "failed", NOT_EXTRACTED,
        ):
            by_tag[f.tag] = v.value
    out = dict(values)
    for fid, derived in compute_derived(fields, by_tag).items():
        out[fid] = ValueView(
            field_id=fid, status="derived" if derived.value is not None else "not_found",
            value=derived.value, value_as_read=None, confidence=None, basis=None,
            read_id=None, provenance={}, problems=(), review=None,
            derived_from=derived.inputs, missing_inputs=derived.missing,
        )
    return out


def _column(f: FieldDefinition) -> FieldColumn:
    return FieldColumn(field_id=f.field_id, name=f.name, tag=f.tag, origin=f.origin,
                       data_type=f.data_type, unit=f.unit)


def _scalar(value: object) -> object:
    """A cell for CSV: the current period of a {current, prior} pair."""
    if isinstance(value, Mapping):
        return value.get("current")
    return value


# -- the service -------------------------------------------------------------


class ExtractionService:
    """Every method acts as `user_id`. Runs are visible only to the Invitee
    who started them (like jobs); Extracted values belong to Public filings
    and are visible to every Invitee (#16)."""

    def __init__(self, session: AsyncSession, user_id: str, *, max_reports: int = 10) -> None:
        self._s = session
        self._user = user_id
        self._repo = ExtractionRepository(session)
        self._schemas = SchemaService(session, user_id)
        self._max_reports = max_reports

    # -- planning and starting runs ------------------------------------------

    async def plan(
        self,
        *,
        schema_id: str,
        version: int | None,
        report_ids: Sequence[str],
        field_ids: Sequence[str] | None,
        forced_field_ids: Sequence[str] = (),
        client: ExtractorClient | None = None,
    ) -> PlanView:
        """Which Field x Report pairs a run would read now, and why. Nothing
        is read or stored. The engine version is `client`'s, so the answer
        matches what a run started now would do."""
        view = await self._schemas.version(schema_id, version)
        reports = await self._check_reports(report_ids)
        selected = self._select(view, field_ids, forced_field_ids)
        if client is None:
            raise RunRejected("planning needs the extractor to be configured")
        engine = engine_versions(client)
        reads = await self._repo.live_reads(reports, selected)
        by_id = {f.field_id: f for f in view.fields}
        plan = plan_extraction(
            fields=[PlannedField(fid, view.definition_hashes[fid], by_id[fid].origin)
                    for fid in selected],
            doc_ids=reports,
            reads=[StoredRead(r.id, r.doc_id, r.field_id, r.definition_hash,
                              r.prompt_version, r.retrieval_version, r.model, r.status)
                   for r in reads],
            engine=engine, forced=forced_field_ids,
        )
        summary: dict[str, int] = {}
        for item in plan.items:
            summary[item.reason] = summary.get(item.reason, 0) + 1
        return PlanView(
            schema_id=schema_id, version=view.summary.version,
            cells=tuple(PlanCell(i.doc_id, i.field_id, i.action, i.reason) for i in plan.items),
            summary=summary,
        )

    async def create_run(
        self,
        *,
        schema_id: str,
        version: int | None,
        report_ids: Sequence[str],
        field_ids: Sequence[str] | None,
        forced_field_ids: Sequence[str] = (),
        retry_of: str | None = None,
    ) -> str:
        """Record a queued run and return its id. The caller queues the job
        and commits; nothing is read here."""
        view = await self._schemas.version(schema_id, version)
        reports = await self._check_reports(report_ids)
        self._select(view, field_ids, forced_field_ids)
        version_row = await self._repo.get_version(schema_id, view.summary.version)
        assert version_row is not None  # just resolved above
        run_id = str(uuid.uuid4())
        await self._repo.create_run(
            run_id=run_id, schema_version_id=version_row.id, user_id=self._user,
            doc_ids=reports,
            field_ids=None if field_ids is None else list(dict.fromkeys(field_ids)),
            forced_field_ids=list(dict.fromkeys(forced_field_ids)), retry_of=retry_of,
        )
        return run_id

    async def attach_job(self, run_id: str, api_job_id: str) -> None:
        await self._repo.attach_job(run_id, api_job_id)

    async def mark_unqueued(self, run_id: str, error: str) -> None:
        await self._repo.set_run_status(run_id, status="failed", error=error, finished=True)

    async def retry_spec(self, run_id: str) -> tuple[str, int, list[str], list[str]]:
        """(schema id, version, Report ids, Field ids) of a new run covering
        only what `run_id` failed to read. Raises `NothingToRetry`."""
        run = await self._own_run(run_id)
        if run.status not in TERMINAL:
            raise RunRejected(f"run {run_id} is still {run.status}; retry it once it ends")
        items = await self._repo.run_items(run_id)
        failed = [i for i in items if i.outcome == "failed"]
        reports = await self._repo.run_reports(run_id)
        # A Report with no items never got planned (it failed, or the run
        # crashed before reaching it): retry every Field of it.
        planned = {i.doc_id for i in items}
        failed_reports = [r.doc_id for r in reports
                          if r.status != "succeeded" and r.doc_id not in planned]
        if not failed and not failed_reports:
            raise NothingToRetry(f"run {run_id} has no failed Fields to retry")
        version = await self._repo.version_by_id(run.schema_version_id)
        assert version is not None
        doc_ids = list(dict.fromkeys([i.doc_id for i in failed] + failed_reports))
        if failed_reports:  # a Report that never got planned: retry all its Fields
            view = await self._schemas.version_by_id(run.schema_version_id)
            field_ids = list(run.field_ids or [f.field_id for f in view.fields])
        else:
            field_ids = list(dict.fromkeys(i.field_id for i in failed))
        return version.schema_id, version.version, doc_ids, field_ids

    def _select(
        self, view: VersionView, field_ids: Sequence[str] | None, forced: Sequence[str]
    ) -> list[str]:
        known = {f.field_id for f in view.fields}
        if field_ids is not None:
            unknown = [f for f in field_ids if f not in known]
            if unknown:
                raise RunRejected(f"version {view.summary.version} has no Field(s) {unknown[:5]}")
            selected = list(dict.fromkeys(field_ids))
        else:
            selected = [f.field_id for f in view.fields]
        if not selected:
            raise RunRejected("the run selects no Field")
        stray = [f for f in forced if f not in set(selected)]
        if stray:
            raise RunRejected(f"forced Field(s) {stray[:5]} are not in the run")
        return selected

    async def _check_reports(self, report_ids: Sequence[str]) -> list[str]:
        ids = list(dict.fromkeys(report_ids))
        if not ids:
            raise RunRejected("the run names no Report")
        if len(ids) > self._max_reports:
            raise RunRejected(f"a run covers at most {self._max_reports} Reports")
        docs = DocumentRepository(self._s)
        registry = RegistryRepository(self._s)
        for doc_id in ids:
            if await docs.get(doc_id) is None:
                raise ReportNotFound(f"report {doc_id} not found")
            index = await registry.latest_run(doc_id, "index")
            if index is None or index.status != "done":
                raise RunRejected(
                    f"report {doc_id} has not finished indexing; retrieval would find nothing"
                )
        return ids

    # -- reading runs ---------------------------------------------------------

    async def _own_run(self, run_id: str) -> ExtractionRun:
        run = await self._repo.get_run(run_id)
        if run is None or run.user_id != self._user:
            raise RunNotFound(f"extraction run {run_id} not found")
        return run

    async def run_view(self, run_id: str) -> RunView:
        run = await self._own_run(run_id)
        return await self._run_view(run)

    async def list_runs(self, *, limit: int, offset: int) -> list[RunView]:
        return [await self._run_view(r)
                for r in await self._repo.list_runs(self._user, limit=limit, offset=offset)]

    async def _run_view(self, run: ExtractionRun) -> RunView:
        version = await self._repo.version_by_id(run.schema_version_id)
        assert version is not None
        reports = await self._repo.run_reports(run.id)
        return RunView(
            id=run.id, schema_id=version.schema_id, version=version.version,
            status=run.status,
            field_ids=None if run.field_ids is None else tuple(run.field_ids),
            forced_field_ids=tuple(run.forced_field_ids), counts=dict(run.counts),
            reports=tuple(RunReportView(r.doc_id, r.status, dict(r.counts), r.error)
                          for r in reports),
            retry_of=run.retry_of, api_job_id=run.api_job_id, error=run.error,
            created_at=run.created_at, started_at=run.started_at,
            finished_at=run.finished_at,
        )

    async def results(self, run_id: str) -> ResultTable:
        """Reports x Fields for one run: each cell is the read the run used
        (with its current review), derived Fields computed per Report."""
        run = await self._own_run(run_id)
        view = await self._schemas.version_by_id(run.schema_version_id)
        selected = set(run.field_ids or [f.field_id for f in view.fields])
        fields = [f for f in view.fields if f.field_id in selected]
        items = await self._repo.run_items(run.id)
        reads = await self._repo.reads_by_ids(i.read_id for i in items if i.read_id)
        reviews = await self._repo.latest_reviews(reads)
        by_cell = {(i.doc_id, i.field_id): i for i in items}
        rows: list[ReportRow] = []
        for report in await self._repo.run_reports(run.id):
            values: dict[str, ValueView] = {}
            for f in fields:
                if f.origin == "derived":
                    continue
                item = by_cell.get((report.doc_id, f.field_id))
                read = reads.get(item.read_id) if item and item.read_id else None
                values[f.field_id] = _value_view(
                    f, read, reviews.get(read.id) if read else None, view.review_threshold
                )
            rows.append(await self._row(report.doc_id, fields, values))
        return ResultTable(schema_id=view.schema_id, version=view.summary.version,
                           columns=tuple(_column(f) for f in fields), rows=tuple(rows))

    async def report_values(
        self, doc_id: str, *, schema_id: str, version: int | None,
        client: ExtractorClient | None = None,
    ) -> ResultTable:
        """What is known for one Report under one schema version: per Field
        its latest live read made with the CURRENT definition (`stale` when
        the engine has moved on since), else `not_extracted`."""
        view = await self._schemas.version(schema_id, version)
        if await DocumentRepository(self._s).get(doc_id) is None:
            raise ReportNotFound(f"report {doc_id} not found")
        engine = engine_versions(client) if client is not None else None
        extracted = [f for f in view.fields if f.origin != "derived"]
        reads = await self._repo.live_reads([doc_id], [f.field_id for f in extracted])
        best: dict[str, FieldRead] = {}
        for r in reads:  # newest first
            if r.definition_hash != view.definition_hashes.get(r.field_id):
                continue
            current = best.get(r.field_id)
            if current is None or (current.status == "failed" and r.status != "failed"):
                best[r.field_id] = r
        reviews = await self._repo.latest_reviews(r.id for r in best.values())
        values: dict[str, ValueView] = {}
        for f in extracted:
            read = best.get(f.field_id)
            stale = bool(read and engine and (
                read.prompt_version != engine.prompt_version
                or read.retrieval_version != engine.retrieval_version
                or read.model != engine.model
            ))
            values[f.field_id] = _value_view(
                f, read, reviews.get(read.id) if read else None, view.review_threshold,
                stale=stale,
            )
        row = await self._row(doc_id, list(view.fields), values)
        return ResultTable(schema_id=view.schema_id, version=view.summary.version,
                           columns=tuple(_column(f) for f in view.fields), rows=(row,))

    async def _row(
        self, doc_id: str, fields: Sequence[FieldDefinition], values: Mapping[str, ValueView]
    ) -> ReportRow:
        doc = await DocumentRepository(self._s).get(doc_id)
        full = _with_derived(fields, values)
        return ReportRow(
            doc_id=doc_id, doc_key=doc.doc_key if doc else None,
            fiscal_year=doc.fiscal_year if doc else None,
            company_id=doc.company_id if doc else None,
            values=tuple(full[f.field_id] for f in fields if f.field_id in full),
        )

    # -- exports ----------------------------------------------------------------

    @staticmethod
    def to_csv(table: ResultTable) -> str:
        """Reports x Fields as CSV: one row per Report, one column per Field
        (the effective value; the current period of a two-period value)."""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["report_id", "doc_key", "fiscal_year", *(
            f"{c.name} [{c.tag}]" if c.tag else c.name for c in table.columns
        )])
        for row in table.rows:
            by_id = {v.field_id: v for v in row.values}
            writer.writerow([row.doc_id, row.doc_key or "", row.fiscal_year or "", *(
                "" if _scalar(by_id[c.field_id].value) is None
                else _scalar(by_id[c.field_id].value)
                for c in table.columns
            )])
        return buf.getvalue()

    async def extraction_record(self, run_id: str, doc_id: str) -> dict[str, object]:
        """One Report of a run in the extraction-record shape
        `score_pipeline.py --extraction` reads, so pre/post comparisons stay
        apples to apples (derived Fields included, effective values)."""
        table = await self.results(run_id)
        row = next((r for r in table.rows if r.doc_id == doc_id), None)
        if row is None:
            raise ReportNotFound(f"run {run_id} did not cover report {doc_id}")
        cols = {c.field_id: c for c in table.columns}
        records = []
        for v in row.values:
            c = cols[v.field_id]
            source = dict(v.provenance)
            records.append({
                "field_name": c.name, "tag": c.tag or "", "origin": c.origin,
                "value": v.value, "status": v.status,
                "source": {"page": source.get("page"), "snippet": source.get("snippet"),
                           "evidence_ids": source.get("evidence_ids", []),
                           "bounding_regions": source.get("bounding_regions",
                                                          {"current": [], "prior": []})},
                "confidence": v.confidence if v.confidence is not None else 0.0,
            })
        doc = await DocumentRepository(self._s).get(doc_id)
        record = build_extraction_record(
            doc_id=doc_id, doc_key=(doc.doc_key if doc else "") or "",
            version=(doc.version if doc else 0) or 0, results=records,
        )
        basis = next((v.basis for v in row.values if v.basis), None)
        if basis is not None:
            record["basis"] = basis
        return record

    # -- review -----------------------------------------------------------------

    async def review(
        self, read_id: str, *, action: str, value: object = None, note: str = ""
    ) -> ValueView:
        """Accept a value or correct it. The value as read is never touched;
        the review is recorded beside it with who and when."""
        read = await self._repo.get_read(read_id)
        if read is None:
            raise ReadNotFound(f"extracted value {read_id} not found")
        if action not in ("accepted", "corrected"):
            raise RunRejected("action must be 'accepted' or 'corrected'")
        if action == "accepted" and read.status == "failed":
            raise RunRejected("a failed read has no value to accept; correct it instead")
        f, threshold = await self._definition_of(read)
        if action == "corrected":
            if value is None:
                raise RunRejected("a correction needs a value")
            if f is not None:
                problems = validate_value(value, f)
                if problems:
                    raise RunRejected("; ".join(problems))
        review = await self._repo.add_review(
            read_id=read_id, action=action,
            value=value if action == "corrected" else None, note=note, user_id=self._user,
        )
        if f is None:
            f = FieldDefinition(field_id=read.field_id, name=read.field_id,
                                data_type="text", instructions="")
        return _value_view(f, read, review, threshold)

    async def read_history(self, read_id: str) -> list[ReviewView]:
        if await self._repo.get_read(read_id) is None:
            raise ReadNotFound(f"extracted value {read_id} not found")
        return [v for r in await self._repo.reviews_of(read_id)
                if (v := _review_view(r)) is not None]

    async def _definition_of(self, read: FieldRead) -> tuple[FieldDefinition | None, float]:
        """The Field definition a read was made with (any version carrying
        that exact definition), and that version's review threshold."""
        row = (
            await self._s.execute(
                select(ExtractionField, ExtractionSchemaVersion.review_threshold)
                .join(ExtractionSchemaVersion,
                      ExtractionSchemaVersion.id == ExtractionField.version_id)
                .where(ExtractionField.field_id == read.field_id,
                       ExtractionField.definition_hash == read.definition_hash)
                .order_by(ExtractionSchemaVersion.published_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None, 0.0
        field_row, threshold = row
        return field_from_mapping(field_row.definition, field_id=field_row.field_id), threshold


# -- execution (the worker) ------------------------------------------------------


async def execute_run(
    run_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    deps: EngineDeps,
    api_job_id: str | None = None,
    correlation_id: str | None = None,
) -> dict[str, object]:
    """Run `run_id` to a terminal status and return its summary (the job's
    result). See the module docstring for the redelivery contract."""
    async with session_factory() as s:
        repo = ExtractionRepository(s)
        run = await repo.get_run(run_id)
        if run is None:
            raise RunNotFound(f"extraction run {run_id} not found")
        if run.status in TERMINAL:
            return {"run_id": run_id, "status": run.status, "counts": dict(run.counts)}
        view = await SchemaService(s, None).version_by_id(run.schema_version_id)
        selected = list(run.field_ids or [f.field_id for f in view.fields])
        forced = set(run.forced_field_ids)
        reports = [(r.doc_id, r.status) for r in await repo.run_reports(run_id)]
        await repo.set_run_status(run_id, status="running", started=run.started_at is None)
        await s.commit()

    engine_fields = _engine_fields(view, selected)
    derived_ids = [f.field_id for f in view.fields
                   if f.field_id in set(selected) and f.origin == "derived"]
    totals: dict[str, int] = {}
    report_statuses: list[str] = []
    for doc_id, status in reports:
        if status in TERMINAL:
            report_statuses.append(status)
            continue
        try:
            status, counts, error = await _execute_report(
                run_id, doc_id, session_factory=session_factory, deps=deps,
                fields=engine_fields, derived_ids=derived_ids, forced=forced,
                api_job_id=api_job_id, correlation_id=correlation_id,
            )
        except Exception as exc:  # noqa: BLE001 - one Report must not sink the run
            log.exception("extraction_run.report_crashed", run_id=run_id, doc_id=doc_id)
            status, counts, error = "failed", {}, f"{type(exc).__name__}: {exc}"
        async with session_factory() as s:
            await ExtractionRepository(s).set_report_status(
                run_id, doc_id, status=status, counts=counts, error=error
            )
            await s.commit()
        report_statuses.append(status)
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v

    async with session_factory() as s:
        repo = ExtractionRepository(s)
        if not report_statuses or all(st == "failed" for st in report_statuses):
            final = "failed"
        elif all(st == "succeeded" for st in report_statuses):
            final = "succeeded"
        else:
            final = "partial"
        # Totals over EVERY Report, including ones a previous delivery finished.
        all_totals: dict[str, int] = {}
        for r in await repo.run_reports(run_id):
            for k, v in r.counts.items():
                all_totals[k] = all_totals.get(k, 0) + v
        await repo.set_run_status(run_id, status=final, counts=all_totals, finished=True)
        await s.commit()
    log.info("extraction_run.finished", run_id=run_id, status=final, counts=all_totals)
    return {"run_id": run_id, "status": final, "counts": all_totals}


async def _execute_report(
    run_id: str,
    doc_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    deps: EngineDeps,
    fields: Sequence[EngineField],
    derived_ids: Sequence[str],
    forced: set[str],
    api_job_id: str | None,
    correlation_id: str | None,
) -> tuple[str, dict[str, int], str | None]:
    async with session_factory() as s:
        repo = ExtractionRepository(s)
        await repo.set_report_status(run_id, doc_id, status="running", counts={})
        await s.commit()
        doc = await DocumentRepository(s).get(doc_id)
        if doc is None or doc.doc_key is None or doc.version is None:
            return "failed", {}, "the report no longer exists"
        # Forced Fields this run already re-read (a lost delivery) are not
        # forced again: the redelivery must not pay twice.
        already = {
            r.field_id for r in await repo.live_reads([doc_id], sorted(forced))
            if r.run_id == run_id
        }
        ctx = ExtractContext(
            session=s, store=deps.store, embedder=deps.embedder,
            vector_store=deps.vector_store, doc_id=doc_id, doc_key=doc.doc_key,
            version=doc.version, correlation_id=correlation_id, api_job_id=api_job_id,
        )
        results = await extract_fields(
            ctx=ctx, fields=fields, client=deps.client, forced=forced - already,
            run_id=run_id,
        ) if fields else []
        outcomes: dict[str, tuple[str, str | None]] = {}
        items: list[dict[str, object]] = []
        counts: dict[str, int] = {}
        for r in results:
            # A read this very run stored (a lost delivery) is its own work.
            reused = r.action == "reuse" and r.read.run_id != run_id
            outcome = "reused" if reused else r.read.status
            outcomes[r.field_id] = (outcome, r.read.id)
            items.append({"run_id": run_id, "doc_id": doc_id, "field_id": r.field_id,
                          "action": r.action if outcome == "reused" else "extract",
                          "reason": r.reason, "outcome": "pending", "read_id": None})
            counts[outcome] = counts.get(outcome, 0) + 1
        for fid in derived_ids:
            items.append({"run_id": run_id, "doc_id": doc_id, "field_id": fid,
                          "action": "derive", "reason": "derived", "outcome": "derived",
                          "read_id": None})
            counts["derived"] = counts.get("derived", 0) + 1
        await repo.add_items(items)
        await repo.set_item_outcomes(run_id, doc_id, outcomes)
        await s.commit()
    attempted = [o for o, _ in outcomes.values() if o != "reused"]
    failed = counts.get("failed", 0)
    if failed and failed == len(attempted) and not counts.get("reused"):
        return "failed", counts, f"{failed} Field(s) could not be read"
    if failed:
        return "partial", counts, f"{failed} Field(s) could not be read"
    return "succeeded", counts, None
