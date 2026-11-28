"""`/api/v1/extractions` and Extracted values (S3).

* `POST /extractions/plan` -- which Field x Report pairs a run would read
  now and why; nothing is read (what the S8 Extraction agent asks first).
* `POST /extractions` -- queue an Extraction run (a T5 job, admitted under
  the T7 job quota like every expensive request). 202 with the run id.
* `GET /extractions[/{id}]` -- the caller's runs: status, per-Report
  progress, counts by outcome.
* `POST /extractions/{id}/retry` -- a new run covering only what failed.
* `GET /extractions/{id}/results[?format=csv]` -- Reports x Fields.
* `GET /extractions/{id}/reports/{doc_id}/record` -- one Report in the
  extraction-record shape `score_pipeline.py --extraction` reads.
* `GET /reports/{doc_id}/extracted-values` -- what is known for a Report
  under a schema version, whoever's runs read it (Public filings, #16).
* `POST /extracted-values/{id}/review` -- accept or correct a value; the
  value as read is kept beside the correction.

Runs are visible only to the Invitee who started them (another Invitee's run
is a 404, like a job). Everything goes through `ExtractionService`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from fdi.api.auth import CurrentUserDep
from fdi.api.deps import (
    CorrelationIdDep,
    ExtractorClientDep,
    JobDispatcherDep,
    RateLimitsDep,
    SessionDep,
)
from fdi.api.job_submit import submit_job
from fdi.core.extraction_schema import SchemaInputError
from fdi.pipeline.extraction_runs import (
    ExtractionService,
    NothingToRetry,
    PlanView,
    ReadNotFound,
    ReportNotFound,
    ResultTable,
    ReviewView,
    RunNotFound,
    RunRejected,
    RunView,
    ValueView,
)
from fdi.pipeline.extraction_schemas import SchemaNotFound, VersionNotFound

router = APIRouter(tags=["extractions"])



# -- bodies -----------------------------------------------------------------


class ExtractionIn(BaseModel):
    schema_id: str
    version: int | None = Field(default=None, ge=1, description="Default: the latest.")
    report_ids: list[str] = Field(min_length=1, max_length=50)
    field_ids: list[str] | None = Field(
        default=None, description="Default: every Field of the version."
    )
    force_field_ids: list[str] = Field(
        default=[], description="Fields to read again even when their value is fresh."
    )


class ReviewIn(BaseModel):
    action: Literal["accepted", "corrected"]
    value: object = None
    note: str = Field(default="", max_length=4000)


# -- responses --------------------------------------------------------------


class RunAccepted(BaseModel):
    run_id: str
    job_id: str
    correlation_id: str
    status: str


class RunReportOut(BaseModel):
    doc_id: str
    status: str
    counts: dict[str, int]
    error: str | None


class RunOut(BaseModel):
    id: str
    schema_id: str
    version: int
    status: str
    field_ids: list[str] | None
    forced_field_ids: list[str]
    counts: dict[str, int]
    reports: list[RunReportOut]
    retry_of: str | None
    job_id: str | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunListOut(BaseModel):
    runs: list[RunOut]


class PlanCellOut(BaseModel):
    doc_id: str
    field_id: str
    action: str
    reason: str


class PlanOut(BaseModel):
    schema_id: str
    version: int
    to_extract: int
    summary: dict[str, int]
    cells: list[PlanCellOut]


class ReviewOut(BaseModel):
    action: str
    value: object
    note: str
    user_id: str
    created_at: datetime


class ValueOut(BaseModel):
    field_id: str
    status: str
    value: object
    value_as_read: object
    confidence: float | None
    basis: str | None
    read_id: str | None
    provenance: dict[str, object]
    problems: list[str]
    review: ReviewOut | None
    derived_from: list[str]
    missing_inputs: list[str]
    error: str | None
    read_at: datetime | None
    stale: bool


class ColumnOut(BaseModel):
    field_id: str
    name: str
    tag: str | None
    origin: str
    data_type: str
    unit: str | None


class ReportRowOut(BaseModel):
    doc_id: str
    doc_key: str | None
    fiscal_year: str | None
    company_id: str | None
    values: list[ValueOut]


class ResultTableOut(BaseModel):
    schema_id: str
    version: int
    columns: list[ColumnOut]
    rows: list[ReportRowOut]


class ReviewListOut(BaseModel):
    reviews: list[ReviewOut]


# -- mapping ----------------------------------------------------------------


def _run_out(r: RunView) -> RunOut:
    return RunOut(
        id=r.id, schema_id=r.schema_id, version=r.version, status=r.status,
        field_ids=None if r.field_ids is None else list(r.field_ids),
        forced_field_ids=list(r.forced_field_ids), counts=dict(r.counts),
        reports=[RunReportOut(doc_id=p.doc_id, status=p.status, counts=dict(p.counts),
                              error=p.error) for p in r.reports],
        retry_of=r.retry_of, job_id=r.api_job_id, error=r.error, created_at=r.created_at,
        started_at=r.started_at, finished_at=r.finished_at,
    )


def _review_out(r: ReviewView | None) -> ReviewOut | None:
    if r is None:
        return None
    return ReviewOut(action=r.action, value=r.value, note=r.note, user_id=r.user_id,
                     created_at=r.created_at)


def _value_out(v: ValueView) -> ValueOut:
    return ValueOut(
        field_id=v.field_id, status=v.status, value=v.value, value_as_read=v.value_as_read,
        confidence=v.confidence, basis=v.basis, read_id=v.read_id,
        provenance=dict(v.provenance), problems=list(v.problems),
        review=_review_out(v.review), derived_from=list(v.derived_from),
        missing_inputs=list(v.missing_inputs), error=v.error, read_at=v.read_at,
        stale=v.stale,
    )


def _table_out(t: ResultTable) -> ResultTableOut:
    return ResultTableOut(
        schema_id=t.schema_id, version=t.version,
        columns=[ColumnOut(field_id=c.field_id, name=c.name, tag=c.tag, origin=c.origin,
                           data_type=c.data_type, unit=c.unit) for c in t.columns],
        rows=[ReportRowOut(doc_id=r.doc_id, doc_key=r.doc_key, fiscal_year=r.fiscal_year,
                           company_id=r.company_id, values=[_value_out(v) for v in r.values])
              for r in t.rows],
    )


def _plan_out(p: PlanView) -> PlanOut:
    return PlanOut(
        schema_id=p.schema_id, version=p.version,
        to_extract=sum(1 for c in p.cells if c.action == "extract"),
        summary=dict(p.summary),
        cells=[PlanCellOut(doc_id=c.doc_id, field_id=c.field_id, action=c.action,
                           reason=c.reason) for c in p.cells],
    )


def _service(request: Request, session: SessionDep, user_id: str) -> ExtractionService:
    max_reports = getattr(request.app.state, "extraction_run_max_reports", 10)
    return ExtractionService(session, user_id, max_reports=max_reports)


async def _call[T](op: Callable[[], Awaitable[T]]) -> T:
    try:
        return await op()
    except (RunNotFound, ReportNotFound, ReadNotFound, SchemaNotFound,
            VersionNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except NothingToRetry as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (RunRejected, SchemaInputError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _require_extractor(client: object) -> None:
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="extraction is not configured: missing extractor_client "
                   "(set FDI_GPT_ENDPOINT/FDI_GPT_KEY)",
        )


# -- runs -------------------------------------------------------------------


@router.post("/extractions/plan", response_model=PlanOut)
async def plan_extraction(
    payload: ExtractionIn, request: Request, session: SessionDep,
    extractor_client: ExtractorClientDep, user_id: CurrentUserDep,
) -> PlanOut:
    """Which Field x Report pairs a run started now would read, and why
    (missing, definition_changed, prompt/retrieval/model changed, forced,
    previously_failed) -- the rest would be reused for free."""
    _require_extractor(extractor_client)
    service = _service(request, session, user_id)
    plan = await _call(lambda: service.plan(
        schema_id=payload.schema_id, version=payload.version,
        report_ids=payload.report_ids, field_ids=payload.field_ids,
        forced_field_ids=payload.force_field_ids, client=extractor_client,
    ))
    return _plan_out(plan)


@router.post("/extractions", response_model=RunAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_extraction(
    payload: ExtractionIn, request: Request, response: Response, session: SessionDep,
    extractor_client: ExtractorClientDep, dispatcher: JobDispatcherDep,
    limits: RateLimitsDep, user_id: CurrentUserDep, correlation_id: CorrelationIdDep,
) -> RunAccepted:
    """Queue an Extraction run. Only Fields whose value is missing or out of
    date cost a model call; poll `GET /extractions/{run_id}`."""
    _require_extractor(extractor_client)
    service = _service(request, session, user_id)
    return await _start(
        service, session=session, dispatcher=dispatcher, limits=limits,
        response=response, user_id=user_id, correlation_id=correlation_id,
        create=lambda: service.create_run(
            schema_id=payload.schema_id, version=payload.version,
            report_ids=payload.report_ids, field_ids=payload.field_ids,
            forced_field_ids=payload.force_field_ids,
        ),
    )


async def _start(
    service: ExtractionService,
    *,
    session: SessionDep,
    dispatcher: JobDispatcherDep,
    limits: RateLimitsDep,
    response: Response,
    user_id: str,
    correlation_id: str,
    create: Callable[[], Awaitable[str]],
) -> RunAccepted:
    """Validate and record the run FIRST (so a refusal costs no quota), then
    admit and queue its job in the same transaction."""
    run_id = await _call(create)
    try:
        accepted = await submit_job(
            session, dispatcher, limits, response, user_id=user_id, kind="extraction",
            params={"run_id": run_id}, correlation_id=correlation_id,
            before_commit=lambda job_id: service.attach_job(run_id, job_id),
        )
    except Exception:
        # Refused before commit (e.g. 429): the run row rolls back with the
        # job. Committed but never published: fail the run too, or it reads
        # `queued` forever and can never be retried. Best effort -- the
        # original error is what the caller must see.
        with contextlib.suppress(Exception):
            await session.rollback()
            await service.mark_unqueued(run_id, "the run could not be queued")
            await session.commit()
        raise
    return RunAccepted(run_id=run_id, job_id=accepted.job_id,
                       correlation_id=accepted.correlation_id, status=accepted.status)


@router.get("/extractions", response_model=RunListOut)
async def list_extractions(
    request: Request, session: SessionDep, user_id: CurrentUserDep,
    limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0),
) -> RunListOut:
    service = _service(request, session, user_id)
    return RunListOut(runs=[_run_out(r) for r in await service.list_runs(
        limit=limit, offset=offset
    )])


@router.get("/extractions/{run_id}", response_model=RunOut)
async def get_extraction(
    run_id: str, request: Request, session: SessionDep, user_id: CurrentUserDep
) -> RunOut:
    service = _service(request, session, user_id)
    return _run_out(await _call(lambda: service.run_view(run_id)))


@router.post("/extractions/{run_id}/retry", response_model=RunAccepted,
             status_code=status.HTTP_202_ACCEPTED)
async def retry_extraction(
    run_id: str, request: Request, response: Response, session: SessionDep,
    extractor_client: ExtractorClientDep, dispatcher: JobDispatcherDep,
    limits: RateLimitsDep, user_id: CurrentUserDep, correlation_id: CorrelationIdDep,
) -> RunAccepted:
    """A new run reading only the Fields this run failed to read (409 when
    nothing failed). Their failed reads are retried automatically."""
    _require_extractor(extractor_client)
    service = _service(request, session, user_id)
    schema_id, version, doc_ids, field_ids = await _call(lambda: service.retry_spec(run_id))
    return await _start(
        service, session=session, dispatcher=dispatcher, limits=limits,
        response=response, user_id=user_id, correlation_id=correlation_id,
        create=lambda: service.create_run(
            schema_id=schema_id, version=version, report_ids=doc_ids, field_ids=field_ids,
            retry_of=run_id,
        ),
    )


@router.get("/extractions/{run_id}/results", response_model=None)
async def extraction_results(
    run_id: str, request: Request, session: SessionDep, user_id: CurrentUserDep,
    fmt: Literal["json", "csv"] = Query(default="json", alias="format"),
) -> ResultTableOut | Response:
    """Reports x Fields. `format=csv` downloads it: one row per Report, one
    column per Field, effective values (a correction wins)."""
    service = _service(request, session, user_id)
    table = await _call(lambda: service.results(run_id))
    if fmt == "csv":
        return Response(
            content=ExtractionService.to_csv(table), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="extraction-{run_id}.csv"'},
        )
    return _table_out(table)


@router.get("/extractions/{run_id}/reports/{doc_id}/record", response_model=None)
async def extraction_record(
    run_id: str, doc_id: str, request: Request, session: SessionDep, user_id: CurrentUserDep
) -> dict[str, object]:
    """One Report of a run as an extraction record -- feed it to
    `scripts/score_pipeline.py --extraction` to score it."""
    service = _service(request, session, user_id)
    return await _call(lambda: service.extraction_record(run_id, doc_id))


# -- Extracted values ---------------------------------------------------------


@router.get("/reports/{doc_id}/extracted-values", response_model=ResultTableOut)
async def report_extracted_values(
    doc_id: str, request: Request, session: SessionDep, user_id: CurrentUserDep,
    extractor_client: ExtractorClientDep,
    schema_id: str = Query(), version: int | None = Query(default=None, ge=1),
) -> ResultTableOut:
    """Every Field of a schema version for one Report: the latest value read
    with the Field's current definition (`stale` once the engine has moved
    on), `not_extracted` when none exists yet, derived Fields computed."""
    service = _service(request, session, user_id)
    table = await _call(lambda: service.report_values(
        doc_id, schema_id=schema_id, version=version, client=extractor_client
    ))
    return _table_out(table)


@router.post("/extracted-values/{read_id}/review", response_model=ValueOut)
async def review_value(
    read_id: str, payload: ReviewIn, request: Request, session: SessionDep,
    user_id: CurrentUserDep,
) -> ValueOut:
    """Accept or correct one Extracted value. The value as read is never
    changed; the review is recorded beside it with who and when."""
    service = _service(request, session, user_id)
    value = await _call(lambda: service.review(
        read_id, action=payload.action, value=payload.value, note=payload.note
    ))
    await session.commit()
    return _value_out(value)


@router.get("/extracted-values/{read_id}/reviews", response_model=ReviewListOut)
async def value_reviews(
    read_id: str, request: Request, session: SessionDep, user_id: CurrentUserDep
) -> ReviewListOut:
    service = _service(request, session, user_id)
    reviews = await _call(lambda: service.read_history(read_id))
    return ReviewListOut(reviews=[r for v in reviews if (r := _review_out(v)) is not None])
