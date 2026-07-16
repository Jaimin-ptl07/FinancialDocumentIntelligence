"""HTTP surface."""

import contextlib
import uuid
from collections.abc import Awaitable, Callable

import sqlalchemy
import structlog
from fastapi import APIRouter, HTTPException, Response, status

from fdi.api.deps import CorrelationIdDep, DispatcherDep, SessionDep, StoreDep
from fdi.api.schemas import JobAccepted, JobView, ReadyView
from fdi.platform.broker.health import check_broker
from fdi.platform.config import get_settings
from fdi.platform.db.repository import RegistryRepository

log = structlog.get_logger(__name__)
router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness only. Must never touch a backend, or a DB blip restarts the API.

    Takes no `SessionDep`/`StoreDep` *by construction*, which is the only thing
    that actually enforces this -- a dependency here would open a connection
    before the body ran, and the body would still look backend-free.
    """
    return {"status": "ok"}


async def _probe(name: str, check: Callable[[], Awaitable[object]]) -> bool:
    """Run one readiness check. Never raises; a probe that can raise cannot report.

    The `log` call is suppressed for the same reason `api.errors._unhandled`
    suppresses its own: an unconfigured structlog renders through rich, which is
    unencodable on cp1252 stdout, and the UnicodeEncodeError would escape this
    `except` and turn a clean 503 into a bodyless 500 -- i.e. the endpoint whose
    entire job is to report the outage becomes a second outage. This is the only
    class of place that earns the suppression: `/ready` and the 500 handler are
    the two responses that ARE the diagnostic. `pipeline.queued` below is
    deliberately NOT suppressed -- a broken logger on the normal path should be
    loud.

    `warning` + `repr`, not `exception`: an orchestrator polls /ready every few
    seconds, so a full traceback per poll against a down backend is a log flood
    that buries the first one -- which is the only one that says when it started.
    """
    try:
        await check()
        return True
    except Exception as exc:
        with contextlib.suppress(Exception):
            log.warning(f"ready.{name}_failed", error=repr(exc))
        return False


@router.get("/ready", response_model=ReadyView)
async def ready(session: SessionDep, store: StoreDep, response: Response) -> ReadyView:
    """Readiness: can this instance actually serve traffic?

    All three backends are probed because /ping-job needs all three: it writes a
    row, the worker reads the bucket, and the publish goes to the broker. A
    `broker=True` literal -- which is what this endpoint shipped as -- is not a
    cheap approximation of that, it is a field that reports success without
    looking, on the one endpoint whose only product is the looking.
    """
    postgres_ok = await _probe(
        "postgres", lambda: session.execute(sqlalchemy.text("SELECT 1"))
    )
    s3_ok = await _probe("s3", store.ensure_bucket)
    broker_ok = await _probe("broker", lambda: check_broker(get_settings().broker_url))

    view = ReadyView(postgres=postgres_ok, s3=s3_ok, broker=broker_ok)
    if not (postgres_ok and s3_ok and broker_ok):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return view


@router.post("/ping-job", status_code=status.HTTP_202_ACCEPTED, response_model=JobAccepted)
async def create_ping_job(
    session: SessionDep, correlation_id: CorrelationIdDep, dispatcher: DispatcherDep
) -> JobAccepted:
    """Persist the document, publish a "ping" stage run. The worker picks it up.

    The id arrives already bound and already validated from
    `CorrelationIdMiddleware`, so it is known to fit the correlation kwarg's
    varchar(36) once the worker records a `stage_run`. Reading the header here
    instead -- which is where it started -- put an unvalidated client string into
    that column, and PostgreSQL raises rather than truncates: a 37-character
    `X-Request-ID` returned a 500.

    Publishing goes through an injected `StageDispatcher` rather than an inline
    `from fdi.workers.tasks import run_stage_task`. That import was a real
    layering violation, not a formality: `api` and `workers` are independent
    siblings, and a function-local import evades the eye but not the import
    graph -- lint-imports reported it at this line. The seam lives in
    `fdi.pipeline.dispatch`, below both, and the concrete task is chosen in
    `api/app.py`'s lifespan.
    """
    doc_id = str(uuid.uuid4())
    await RegistryRepository(session).create_document(doc_id)
    await session.commit()

    dispatcher.dispatch(doc_id, "ping", correlation_id)
    log.info("pipeline.queued", doc_id=doc_id, stage="ping")

    return JobAccepted(job_id=doc_id, correlation_id=correlation_id, status="queued")


@router.get("/jobs/{job_id}", response_model=JobView)
async def get_job(job_id: str, session: SessionDep, store: StoreDep) -> JobView:
    repo = RegistryRepository(session)
    if await repo.get_document(job_id) is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")

    run = await repo.latest_run(job_id, "ping")
    if run is None:
        # The document exists but the worker has not yet created a stage_run for
        # it -- these tests do not run the worker, so this is the steady state
        # right after `/ping-job` returns 202.
        return JobView(
            job_id=job_id, status="queued", artifact_key=None, artifact_url=None, error=None
        )

    artifact_url = None
    if run.artifact_key:
        artifact_url = await store.presigned_get_url(
            run.artifact_key, ttl_seconds=get_settings().artifact_url_ttl_seconds
        )

    return JobView(
        job_id=job_id,
        status=run.status,
        artifact_key=run.artifact_key,
        artifact_url=artifact_url,
        error=run.error,
    )