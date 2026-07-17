"""HTTP surface."""

import contextlib
import uuid
from collections.abc import Awaitable, Callable

import sqlalchemy
import structlog
from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile, status

from fdi.api.deps import (
    CandidatesDep,
    CorrelationIdDep,
    DispatcherDep,
    ResolverDep,
    SessionDep,
    SessionFactoryDep,
    StoreDep,
)
from fdi.api.schemas import (
    DocumentVersionsView,
    DocumentView,
    EmbeddingTriggerView,
    EvalAccepted,
    EvalRequest,
    JobAccepted,
    JobView,
    LeaderboardEntry,
    LeaderboardView,
    OcrTriggerView,
    ReadyView,
    ReprocessView,
    SemanticTriggerView,
    StageStatusView,
    StagesView,
    StaleView,
)
from fdi.pipeline.eval_service import run_evaluation
from fdi.pipeline.intake import intake_document
from fdi.platform.broker.health import check_broker
from fdi.platform.config import get_settings
from fdi.platform.db.models import Document
from fdi.platform.db.repository import DocumentRepository, EvalRepository, RegistryRepository

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


@router.get("/docs/{doc_id}/stale", response_model=StaleView)
async def get_stale(doc_id: str, session: SessionDep, resolver: ResolverDep) -> StaleView:
    """Which stages are stale for this document, per the resolver's topology.

    Staleness is decided from recorded facts (stage_run rows), never by
    reading artifact bodies -- see `fdi.pipeline.resolver`.
    """
    facts = await RegistryRepository(session).runs_for_document(doc_id)
    return StaleView(stale=resolver.stale(facts))


@router.post(
    "/docs/{doc_id}/reprocess",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReprocessView,
)
async def reprocess(
    doc_id: str,
    session: SessionDep,
    correlation_id: CorrelationIdDep,
    dispatcher: DispatcherDep,
    from_: str = Query(alias="from"),
) -> ReprocessView:
    """Dispatch `from_stage`; its successors chain themselves once it finishes.

    A thin slice of `resolver.plan(from_stage)`: only the requested stage is
    published here, not its whole downstream plan.
    """
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    dispatcher.dispatch(doc_id, from_, correlation_id)
    return ReprocessView(dispatched=[from_])


@router.post("/eval", response_model=EvalAccepted)
async def run_eval_endpoint(
    body: EvalRequest,
    session_factory: SessionFactoryDep,
    store: StoreDep,
    candidates: CandidatesDep,
) -> EvalAccepted:
    """Run one candidate against one dataset version, then persist + return it.

    A missing dataset is not special-cased here: `run_evaluation` calls
    `get_dataset` before it looks up the candidate, and a missing artifact
    surfaces as whatever `ArtifactStore.get_json` raises -- left to become a
    500 for this thin slice, since there is no dataset-upload endpoint yet to
    make a 404 for "no such dataset" meaningfully actionable.
    """
    run_id = str(uuid.uuid4())
    try:
        aggregate = await run_evaluation(
            run_id=run_id,
            candidate_name=body.candidate,
            dataset_name=body.dataset,
            dataset_version=body.dataset_version,
            session_factory=session_factory,
            store=store,
            candidates=candidates,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"candidate {body.candidate!r} not found"
        ) from exc
    return EvalAccepted(run_id=run_id, aggregate=aggregate)


@router.get("/datasets/{name}/leaderboard", response_model=LeaderboardView)
async def leaderboard_endpoint(
    name: str, session: SessionDep, metric: str = Query(default="exact_match")
) -> LeaderboardView:
    rows = await EvalRepository(session).leaderboard(name, metric)
    return LeaderboardView(
        leaderboard=[
            LeaderboardEntry(candidate=c, version=v, aggregate=a) for (c, v, a) in rows
        ]
    )


# --------------------------------------------------------------------------
# Task 6: document intake (upload + reads).
# --------------------------------------------------------------------------


def _view(doc: Document) -> DocumentView:
    return DocumentView(
        id=doc.id,
        checksum=doc.checksum,
        filename=doc.filename,
        byte_size=doc.byte_size,
        doc_key=doc.doc_key,
        version=doc.version,
        content_type=doc.content_type,
    )


@router.post("/documents", status_code=status.HTTP_201_CREATED, response_model=DocumentView)
async def upload_document(
    session_factory: SessionFactoryDep,
    store: StoreDep,
    file: UploadFile = File(...),
    doc_key: str | None = Form(default=None),
) -> DocumentView:
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=415, detail="only application/pdf is accepted")
    data = await file.read()
    cap = get_settings().max_upload_mb * 1024 * 1024
    if len(data) > cap:
        raise HTTPException(
            status_code=413, detail=f"file exceeds {get_settings().max_upload_mb} MB"
        )
    doc = await intake_document(
        pdf=data,
        filename=file.filename or "upload.pdf",
        doc_key=doc_key or file.filename or "upload.pdf",
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    return _view(doc)


@router.get("/documents/{doc_id}", response_model=DocumentView)
async def get_document(doc_id: str, session: SessionDep) -> DocumentView:
    doc = await DocumentRepository(session).get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    return _view(doc)


@router.get("/documents", response_model=DocumentVersionsView)
async def list_documents(session: SessionDep, doc_key: str) -> DocumentVersionsView:
    docs = await DocumentRepository(session).list_by_doc_key(doc_key)
    return DocumentVersionsView(versions=[_view(d) for d in docs])


@router.post(
    "/documents/{doc_id}/ocr",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OcrTriggerView,
)
async def trigger_ocr(
    doc_id: str, session: SessionDep, correlation_id: CorrelationIdDep, dispatcher: DispatcherDep
) -> OcrTriggerView:
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    dispatcher.dispatch(doc_id, "ocr", correlation_id)
    return OcrTriggerView(doc_id=doc_id, stage="ocr")


@router.post(
    "/documents/{doc_id}/semantic",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=SemanticTriggerView,
)
async def trigger_semantic(
    doc_id: str, session: SessionDep, correlation_id: CorrelationIdDep, dispatcher: DispatcherDep
) -> SemanticTriggerView:
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    dispatcher.dispatch(doc_id, "semantic", correlation_id)
    return SemanticTriggerView(doc_id=doc_id, stage="semantic")


@router.post(
    "/documents/{doc_id}/embedding",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EmbeddingTriggerView,
)
async def trigger_embedding(
    doc_id: str, session: SessionDep, correlation_id: CorrelationIdDep, dispatcher: DispatcherDep
) -> EmbeddingTriggerView:
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    dispatcher.dispatch(doc_id, "embedding", correlation_id)
    return EmbeddingTriggerView(doc_id=doc_id, stage="embedding")


# --------------------------------------------------------------------------
# Task 1 (SP-18): per-document stage status + artifact-body proxy.
# --------------------------------------------------------------------------


@router.get("/documents/{doc_id}/stages", response_model=StagesView)
async def document_stages(doc_id: str, session: SessionDep) -> StagesView:
    repo = RegistryRepository(session)
    if await repo.get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    runs = await repo.latest_runs(doc_id)
    return StagesView(
        stages=[
            StageStatusView(name=name, status=run.status, error=run.error)
            for name, run in sorted(runs.items())
        ]
    )


@router.get("/documents/{doc_id}/artifacts/{stage}")
async def document_artifact(
    doc_id: str, stage: str, session: SessionDep, store: StoreDep
) -> dict[str, object]:
    run = await RegistryRepository(session).latest_run(doc_id, stage)
    if run is None or run.artifact_key is None:
        raise HTTPException(status_code=404, detail=f"no {stage} artifact for document {doc_id}")
    body = await store.get_json(run.artifact_key)
    return dict(body)