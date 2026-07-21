"""HTTP surface."""

import contextlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import cast
from urllib.parse import quote

import sqlalchemy
import structlog
from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile, status

from fdi.api.deps import (
    AgentClientDep,
    CandidatesDep,
    CorrelationIdDep,
    DispatcherDep,
    EmbedderDep,
    EntityThresholdDep,
    ExtractionSchemaPathDep,
    ExtractorClientDep,
    GraphStoreDep,
    ResolverDep,
    SessionDep,
    SessionFactoryDep,
    StoreDep,
    VectorStoreDep,
)
from fdi.api.schemas import (
    AnalyticsView,
    AskRequest,
    AskResponse,
    ConceptCompareRequest,
    ConceptComparisonView,
    ConceptLookupView,
    CorpusTotalsView,
    DocumentListView,
    DocumentStructureView,
    DocumentSummaryView,
    DocumentVersionsView,
    DocumentView,
    EmbeddingTriggerView,
    EntitiesView,
    EntityResolveRequest,
    EntityTimelineView,
    EvalAccepted,
    EvalRequest,
    ExtractDocumentView,
    ExtractRequest,
    GraphLoadTriggerView,
    GraphNeighborsRequest,
    GraphNeighborsView,
    HybridSearchRequest,
    HybridSearchResultsView,
    HybridSearchResultView,
    JobAccepted,
    JobView,
    LeaderboardEntry,
    LeaderboardView,
    OcrTriggerView,
    ReadingOrderView,
    ReadyView,
    ReprocessView,
    SchemaValidateRequest,
    SchemaValidationView,
    SchemaView,
    SchemaWriteRequest,
    SearchRequest,
    SearchResultsView,
    SearchResultView,
    SemanticTriggerView,
    StageStatusView,
    StagesView,
    StageThroughputView,
    StaleView,
    TablesView,
    VectorSearchRequest,
    VectorSearchResultsView,
    VectorSearchResultView,
)
from fdi.core.extractor import parse_field_specs
from fdi.core.ontology import CanonicalConcept
from fdi.core.schema import bump_patch_version, validate_extraction_schema
from fdi.pipeline.agent_tools import AgentContext
from fdi.pipeline.ask import answer_question
from fdi.pipeline.crossdoc import resolve_entities
from fdi.pipeline.entities import compare_concept_across_versions, entity_timeline
from fdi.pipeline.eval_service import run_evaluation
from fdi.pipeline.extractor import ExtractContext, extract_document
from fdi.pipeline.graphquery import (
    document_structure as document_structure_query,
)
from fdi.pipeline.graphquery import (
    neighbors as neighbors_query,
)
from fdi.pipeline.graphquery import (
    reading_order as reading_order_query,
)
from fdi.pipeline.graphstore import GraphStore
from fdi.pipeline.intake import document_key, intake_document
from fdi.pipeline.retrieval import hybrid_search, vector_search
from fdi.pipeline.schema_store import load_schema, save_schema
from fdi.pipeline.tables import concept_lookup, get_tables
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


@router.get("/documents", response_model=None)
async def list_documents(
    session: SessionDep,
    doc_key: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> DocumentVersionsView | DocumentListView:
    """Two modes on one path.

    `?doc_key=` -> every version of that key, ascending, exactly as this
    route has always behaved (SP-17's extractor flow and the SP-18 frontend
    both call it that way; changing the shape would break them).

    No `doc_key` -> the corpus list a real UI needs: the latest version per
    doc_key, each with a stage summary, paginated. `response_model=None` +
    a union return annotation is what lets one path serve both: a declared
    `response_model` would coerce one shape into the other and silently drop
    half of each.
    """
    repo = DocumentRepository(session)
    if doc_key is not None:
        docs = await repo.list_by_doc_key(doc_key)
        return DocumentVersionsView(versions=[_view(d) for d in docs])

    rows, total = await repo.list_latest_per_doc_key(limit=limit, offset=offset)
    counts = await RegistryRepository(session).latest_run_status_counts([d.id for d in rows])
    return DocumentListView(
        documents=[
            DocumentSummaryView(
                id=d.id,
                checksum=d.checksum,
                filename=d.filename,
                byte_size=d.byte_size,
                doc_key=d.doc_key,
                version=d.version,
                content_type=d.content_type,
                stage_counts=counts.get(d.id, {}),
            )
            for d in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


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


@router.post(
    "/documents/{doc_id}/graph_load",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=GraphLoadTriggerView,
)
async def trigger_graph_load(
    doc_id: str, session: SessionDep, correlation_id: CorrelationIdDep, dispatcher: DispatcherDep
) -> GraphLoadTriggerView:
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    dispatcher.dispatch(doc_id, "graph_load", correlation_id)
    return GraphLoadTriggerView(doc_id=doc_id, stage="graph_load")


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


def _content_disposition(filename: str | None) -> str:
    """`inline` + BOTH filename forms (RFC 6266).

    `filename=` must be a quoted ASCII token: a real filing named
    "ACME -- FY24.pdf" with an em dash puts a non-latin-1 byte into a header
    value, which the ASGI server rejects outright. So the plain form is
    sanitized (every non-ASCII / quote / control byte -> "_") and the true
    name is carried alongside in the RFC 5987 `filename*` form, which every
    current browser prefers when present. Absent filename -> "document.pdf".
    """
    name = filename or "document.pdf"
    ascii_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


@router.get("/documents/{doc_id}/file")
async def document_file(doc_id: str, session: SessionDep, store: StoreDep) -> Response:
    """The source PDF, proxied through this API rather than presigned.

    SP-18 sec 2 decision 1: a browser fetching MinIO directly would need
    MinIO's own CORS configured, so artifact bodies come through the
    already-CORS'd API instead. An embedded PDF viewer is exactly that case,
    so it keeps the same posture -- `presigned_get_url` stays available on
    /jobs for direct downloads.

    HTTP range support is DEFERRED, not forgotten: `ArtifactStore.get_raw`
    is built around reading a whole body and then close()/release_conn()
    (see its own comment on what leaks once the body stops being read
    whole), so ranges need a new streaming `get_range` on the storage layer
    plus Range/If-Range parsing and 206/416 handling. Until then this
    deliberately emits NO `Accept-Ranges` header -- advertising `bytes` and
    then ignoring the header is what makes a viewer render garbage.
    """
    doc = await DocumentRepository(session).get(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    if not doc.checksum:
        raise HTTPException(
            status_code=404, detail=f"no source file for document {doc_id} (no checksum)"
        )
    key = document_key(doc.checksum)
    if not await store.exists(key):
        raise HTTPException(
            status_code=404, detail=f"no source file for document {doc_id} at {key}"
        )
    data = await store.get_bytes(key)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _content_disposition(doc.filename),
            "Content-Length": str(len(data)),
        },
    )


# --------------------------------------------------------------------------
# Task 6 (SP-14): POST /search -- the first synchronous, task-free route.
# --------------------------------------------------------------------------


@router.post("/search", response_model=SearchResultsView)
async def search(
    body: SearchRequest, embedder: EmbedderDep, vector_store: VectorStoreDep
) -> SearchResultsView:
    missing = [
        name
        for name, svc in (("embedder", embedder), ("vector_store", vector_store))
        if svc is None
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=(
                f"search is not configured: missing {', '.join(missing)} "
                "(set FDI_MILVUS_URI -- embeddings run locally via BGE and need no endpoint/key)"
            ),
        )
    # `missing` above is a runtime check across a name/value tuple, which
    # mypy --strict cannot use to narrow `embedder`/`vector_store` themselves
    # (it only sees `svc is None` inside a comprehension, not a check against
    # either specific variable) -- these asserts are the narrowing, not a new
    # runtime possibility: `missing` being empty already guarantees both are
    # non-None.
    assert embedder is not None
    assert vector_store is not None
    [vector] = embedder.embed([body.query])
    hits = vector_store.search(vector, body.k)
    return SearchResultsView(
        results=[
            SearchResultView(
                id=str(h["id"]),
                score=float(h["score"]),  # type: ignore[arg-type]
                metadata=dict(cast(Mapping[str, object], h["metadata"])),
            )
            for h in hits
        ]
    )


# --------------------------------------------------------------------------
# RT-2: POST /retrieve/vector -- semantic search WITH text + citations,
# filterable by section_type (Milvus metadata) and/or doc_key/version (a
# Postgres join). POST /search above is UNCHANGED -- design spec
# docs/superpowers/specs/2026-07-18-rt2-vector-retrieval-design.md sec 2
# decision 5 -- /search stays the raw, never-dropping probe it always was;
# this route is the additive, citation-first tool the retrieval roadmap's
# vector_search(query, filters, k) describes.
# --------------------------------------------------------------------------


@router.post("/retrieve/vector", response_model=VectorSearchResultsView)
async def retrieve_vector(
    body: VectorSearchRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
) -> VectorSearchResultsView:
    missing = [
        name
        for name, svc in (("embedder", embedder), ("vector_store", vector_store))
        if svc is None
    ]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=(
                f"retrieve/vector is not configured: missing {', '.join(missing)} "
                "(set FDI_MILVUS_URI -- embeddings run locally via BGE and need no endpoint/key)"
            ),
        )
    assert embedder is not None
    assert vector_store is not None
    filters = {
        "section_type": body.filters.section_type if body.filters else None,
        "doc_key": body.filters.doc_key if body.filters else None,
        "version": body.filters.version if body.filters else None,
    }
    results = await vector_search(
        session=session,
        embedder=embedder,
        vector_store=vector_store,
        query=body.query,
        k=body.k,
        filters=filters,
    )
    return VectorSearchResultsView(
        results=[VectorSearchResultView.model_validate(r) for r in results]
    )


# --------------------------------------------------------------------------
# Task 6 (SP-16): POST /entities/resolve -- cross-document entity resolution.
# --------------------------------------------------------------------------


@router.post("/entities/resolve", response_model=EntitiesView)
async def resolve_entities_route(
    body: EntityResolveRequest,
    session: SessionDep,
    store: StoreDep,
    default_threshold: EntityThresholdDep,
) -> EntitiesView:
    """Corpus-level cross-document entity resolution (on-demand, task-free like
    /search). Clusters documents on embedding similarity and returns entity
    records; body `threshold` overrides the configured default for this call.
    """
    threshold = body.threshold if body.threshold is not None else default_threshold
    result = await resolve_entities(session=session, store=store, threshold=threshold)
    return EntitiesView.model_validate(result)


# --------------------------------------------------------------------------
# RT-7: cross-document / entity retrieval -- doc_key-scoped filing timeline
# and cross-version concept comparison. `doc_key` IS the entity identity here
# (design spec docs/superpowers/specs/2026-07-18-rt7-crossdoc-entity-
# retrieval-design.md, decision 1) -- distinct from POST /entities/resolve's
# corpus-wide, embedding-similarity `entity_id`, which is recomputed per call
# and therefore not a stable route identifier.
# --------------------------------------------------------------------------


@router.get("/entities/{doc_key}/timeline", response_model=EntityTimelineView)
async def entity_timeline_route(
    doc_key: str, session: SessionDep, store: StoreDep
) -> EntityTimelineView:
    result = await entity_timeline(session=session, store=store, doc_key=doc_key)
    if result is None:
        raise HTTPException(status_code=404, detail=f"no documents for doc_key {doc_key!r}")
    return EntityTimelineView.model_validate(result)


@router.post("/entities/compare", response_model=ConceptComparisonView)
async def compare_concept_route(
    body: ConceptCompareRequest, session: SessionDep, store: StoreDep
) -> ConceptComparisonView:
    result = await compare_concept_across_versions(
        session=session, store=store, doc_key=body.doc_key, concept=body.concept
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"no documents for doc_key {body.doc_key!r}")
    return ConceptComparisonView.model_validate(result)


# --------------------------------------------------------------------------
# RT-6: structured financial retrieval over the `structure` artifact's
# canonicalized table grids. No new stage/artifact -- reads `structure` on
# demand via fdi.pipeline.tables, the same shape resolve_chunk (RT-1) reads
# `chunks`.
# --------------------------------------------------------------------------


@router.get("/documents/{doc_id}/tables", response_model=TablesView)
async def get_tables_route(
    doc_id: str, session: SessionDep, store: StoreDep, keyword: str | None = Query(default=None)
) -> TablesView:
    tables = await get_tables(session=session, store=store, doc_id=doc_id, keyword=keyword)
    if tables is None:
        raise HTTPException(
            status_code=404, detail=f"no structure artifact for document {doc_id}"
        )
    return TablesView.model_validate({"doc_id": doc_id, "tables": tables})


@router.get("/documents/{doc_id}/concepts/{concept}", response_model=ConceptLookupView)
async def concept_lookup_route(
    doc_id: str, concept: CanonicalConcept, session: SessionDep, store: StoreDep
) -> ConceptLookupView:
    rows = await concept_lookup(session=session, store=store, doc_id=doc_id, concept=concept)
    if rows is None:
        raise HTTPException(
            status_code=404, detail=f"no structure artifact for document {doc_id}"
        )
    return ConceptLookupView.model_validate(
        {"doc_id": doc_id, "concept": concept.value, "rows": rows}
    )


# --------------------------------------------------------------------------
# RT-9: POST /retrieve -- hybrid (RRF) retrieval over RT-2's vector_search +
# RT-9's bm25_search. `/documents/{id}/index_fts` and `/retrieve/keyword`
# (RT-3's Postgres-only keyword channel, plainto_tsquery + ts_rank) are
# REMOVED here along with keyword_search/index_chunks_fts themselves --
# plainto_tsquery ANDed every lexeme, so every real (sentence-shaped) query
# returned zero hits. This route's own 503-when-unconfigured handling (to
# match /retrieve/vector, now that both its channels share one VectorStore
# seam) is a later task's job, not reinstated here.
# --------------------------------------------------------------------------


@router.post("/retrieve", response_model=HybridSearchResultsView)
async def hybrid_search_route(
    body: HybridSearchRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
) -> HybridSearchResultsView:
    filters = {
        "section_type": body.filters.section_type if body.filters else None,
        "doc_key": body.filters.doc_key if body.filters else None,
        "version": body.filters.version if body.filters else None,
    }
    results = await hybrid_search(
        session=session,
        embedder=embedder,
        vector_store=vector_store,
        query=body.query,
        k=body.k,
        filters=filters,
    )
    return HybridSearchResultsView(
        results=[HybridSearchResultView.model_validate(r) for r in results]
    )


# --------------------------------------------------------------------------
# RT-5: graph retrieval -- neighbors / document structure / reading order.
# --------------------------------------------------------------------------


def _require_graph_store(graph_store: GraphStore | None) -> GraphStore:
    if graph_store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "graph retrieval is not configured: missing graph_store "
                "(set FDI_NEO4J_URI/FDI_NEO4J_PASSWORD)"
            ),
        )
    return graph_store


@router.post("/graph/neighbors", response_model=GraphNeighborsView)
async def graph_neighbors(
    body: GraphNeighborsRequest, session: SessionDep, store: StoreDep, graph_store: GraphStoreDep
) -> GraphNeighborsView:
    gs = _require_graph_store(graph_store)
    result = await neighbors_query(
        session=session, store=store, graph_store=gs, node_id=body.node_id
    )
    return GraphNeighborsView.model_validate(result)


@router.get("/documents/{doc_id}/graph/structure", response_model=DocumentStructureView)
async def document_structure_route(
    doc_id: str, session: SessionDep, store: StoreDep, graph_store: GraphStoreDep
) -> DocumentStructureView:
    gs = _require_graph_store(graph_store)
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    result = await document_structure_query(
        session=session, store=store, graph_store=gs, doc_id=doc_id
    )
    return DocumentStructureView.model_validate(result)


@router.get("/documents/{doc_id}/graph/reading-order", response_model=ReadingOrderView)
async def reading_order_route(
    doc_id: str, session: SessionDep, store: StoreDep, graph_store: GraphStoreDep
) -> ReadingOrderView:
    gs = _require_graph_store(graph_store)
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    result = await reading_order_query(
        session=session, store=store, graph_store=gs, doc_id=doc_id
    )
    return ReadingOrderView.model_validate(result)


# --------------------------------------------------------------------------
# RT-8: POST /ask -- the retrieval agent. Plans over TOOL_SCHEMAS (every
# RT-2..RT-7/SP-16 tool registered via fdi.pipeline.agent_tools), executes
# against the SAME injected services /retrieve, /graph/*, /entities/* use,
# and returns a cited answer. 503s ONLY when agent_client is unconfigured --
# an unconfigured embedder/vector_store/graph_store degrades tool-by-tool
# (design spec decision 10), not at the route.
# --------------------------------------------------------------------------


@router.post("/ask", response_model=AskResponse)
async def ask(
    body: AskRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
    graph_store: GraphStoreDep,
    entity_threshold: EntityThresholdDep,
    agent_client: AgentClientDep,
) -> AskResponse:
    if agent_client is None:
        raise HTTPException(
            status_code=503,
            detail="ask is not configured: missing agent_client (set FDI_GPT_ENDPOINT/FDI_GPT_KEY)",
        )
    ctx = AgentContext(
        session=session, store=store, embedder=embedder, vector_store=vector_store,
        graph_store=graph_store, default_entity_threshold=entity_threshold,
    )
    max_rounds = body.max_rounds if body.max_rounds is not None else 5
    result = await answer_question(
        question=body.question, ctx=ctx, agent_client=agent_client, max_rounds=max_rounds,
    )
    return AskResponse.model_validate(result)


# --------------------------------------------------------------------------
# SP-17: POST /extract/document -- schema-driven RAG field extraction,
# built on the retrieval layer (RT-1..RT-8). Reads FDI_EXTRACTION_SCHEMA_
# PATH, parses it (fdi.core.extractor.parse_field_specs, pure), resolves
# doc_key to its latest document version ONCE (doc_id/doc_key/version all
# thread into ExtractContext, so extract_field can scope hybrid_search by
# {doc_key, version} directly without a per-field DocumentRepository
# lookup), and answers every rag_extractable document_content field of the
# given module (or the whole schema when module is omitted) via
# fdi.pipeline.extractor.extract_document. 503s ONLY when extractor_client
# is unconfigured -- an unconfigured embedder/vector_store degrades each
# field's pool (fewer candidates), not the route (spec decision 10, same
# posture as /ask's tool-level degradation).
# --------------------------------------------------------------------------


@router.post("/extract/document", response_model=ExtractDocumentView)
async def extract_document_route(
    body: ExtractRequest,
    session: SessionDep,
    store: StoreDep,
    embedder: EmbedderDep,
    vector_store: VectorStoreDep,
    extractor_client: ExtractorClientDep,
    schema_path: ExtractionSchemaPathDep,
) -> ExtractDocumentView:
    if extractor_client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "extract/document is not configured: missing extractor_client "
                "(set FDI_GPT_ENDPOINT/FDI_GPT_KEY)"
            ),
        )
    versions = await DocumentRepository(session).list_by_doc_key(body.doc_key)
    if not versions:
        raise HTTPException(status_code=404, detail=f"no documents for doc_key {body.doc_key!r}")
    doc = versions[-1]  # list_by_doc_key orders by version ascending -> last is latest
    version = doc.version
    if version is None:
        # Document.version is nullable at the column level (early intake
        # rows), but list_by_doc_key only ever returns rows created through
        # intake_document, which always sets a real version -- this is an
        # unexpected-state guard, not a normal 404/503 path.
        raise HTTPException(
            status_code=500, detail=f"document {doc.id} has no version"
        )

    schema, _source = await load_schema(store=store, seed_path=schema_path)
    fields = parse_field_specs(schema, body.module)

    ctx = ExtractContext(
        session=session, store=store, embedder=embedder, vector_store=vector_store,
        doc_id=doc.id, doc_key=body.doc_key, version=version,
    )
    result = await extract_document(ctx=ctx, fields=fields, client=extractor_client)
    return ExtractDocumentView.model_validate(
        {"doc_key": body.doc_key, "module": body.module, **result}
    )


# --------------------------------------------------------------------------
# SP-19 (B5): the extraction schema is EDITABLE over HTTP.
#
# Writes persist to an object in the artifact bucket, never back to
# docs/architecture/field-extraction-schema.json: backend/Dockerfile COPYs
# that file into the image at build time, so an in-container write vanishes
# on the next `up` -- or succeeds and leaves each replica with a private
# schema. See fdi.pipeline.schema_store's module docstring. The on-disk file
# stays the immutable v3.1.0 seed. POST /extract/document reads through the
# same load_schema, so a PUT takes effect on the very next extraction.
# --------------------------------------------------------------------------


def _schema_counts(schema: Mapping[str, object]) -> tuple[int, int, int]:
    """(module_count, unit_count, field_count). field_count is what
    parse_field_specs ACTUALLY yields, not the schema's own `field_count`
    hints -- the number that matters is how many fields extraction will
    attempt, which is the number that silently went to zero when the schema
    moved to v3.1."""
    modules_raw = schema.get("modules")
    modules = [m for m in modules_raw if isinstance(m, Mapping)] if isinstance(
        modules_raw, list
    ) else []
    unit_count = 0
    for m in modules:
        units = m.get("retrieval_units")
        if isinstance(units, list):
            unit_count += sum(1 for u in units if isinstance(u, Mapping))
    return len(modules), unit_count, len(parse_field_specs(schema))


def _schema_view(schema: Mapping[str, object], source: str) -> SchemaView:
    version = schema.get("version")
    module_count, unit_count, field_count = _schema_counts(schema)
    return SchemaView(
        version=version if isinstance(version, str) else "",
        source=source,
        module_count=module_count,
        unit_count=unit_count,
        field_count=field_count,
        schema_document=dict(schema),
    )


@router.get("/schema", response_model=SchemaView)
async def get_schema(store: StoreDep, schema_path: ExtractionSchemaPathDep) -> SchemaView:
    """The current field-extraction schema document, whole.

    Read-through: the artifact bucket's copy if one has been saved, else the
    on-disk seed. This route never writes -- a GET that seeded the bucket
    would make the first read of a fresh environment a mutation, and two
    concurrent cold reads a race.
    """
    schema, source = await load_schema(store=store, seed_path=schema_path)
    return _schema_view(schema, source)


@router.put("/schema", response_model=SchemaView)
async def put_schema(
    body: SchemaWriteRequest, store: StoreDep, schema_path: ExtractionSchemaPathDep
) -> SchemaView:
    """Replace the schema. Validated, version-stamped, and concurrency-checked.

    `base_version` must equal the CURRENT document's version or the write is
    refused 409 with both versions named -- two people editing the same
    schema in two browser tabs must not silently clobber each other, and the
    loser needs to know what it should have been based on.

    The new version is stamped SERVER-side by bumping the patch component
    (3.1.0 -> 3.1.1); any `version` inside the submitted document is
    overwritten. A client-chosen version is a second thing to validate and to
    disagree about, and a monotonic patch bump is exactly what an edit-in-a-UI
    produces.

    A document that fails structural validation is refused 422 with one
    JSON-path-prefixed error per problem, and NOTHING is written: a saved
    schema that parses to zero fields would turn POST /extract/document into
    a silent no-op with a 200 response, which is the precise failure this
    endpoint's own program exists to have fixed once already.
    """
    current, _source = await load_schema(store=store, seed_path=schema_path)
    current_version = current.get("version")
    current_version_str = current_version if isinstance(current_version, str) else ""
    if body.base_version != current_version_str:
        raise HTTPException(
            status_code=409,
            detail=(
                f"schema was modified: base_version {body.base_version!r} is not the "
                f"current version {current_version_str!r} -- re-read GET /schema and reapply"
            ),
        )

    errors = validate_extraction_schema(body.schema_document)
    if errors:
        raise HTTPException(
            status_code=422, detail={"message": "schema is invalid", "errors": errors}
        )

    new_doc = dict(body.schema_document)
    new_doc["version"] = bump_patch_version(current_version_str)
    await save_schema(store=store, doc=new_doc)
    log.info(
        "schema.updated",
        base_version=body.base_version,
        version=new_doc["version"],
        field_count=len(parse_field_specs(new_doc)),
    )
    return _schema_view(new_doc, "store")


@router.post("/schema/validate", response_model=SchemaValidationView)
async def validate_schema_route(body: SchemaValidateRequest) -> SchemaValidationView:
    """Dry-run the PUT gate. Touches no storage, so a UI can validate on every
    keystroke without a write path anywhere near it."""
    errors = validate_extraction_schema(body.schema_document)
    module_count, unit_count, field_count = _schema_counts(body.schema_document)
    return SchemaValidationView(
        valid=not errors,
        errors=errors,
        module_count=module_count,
        unit_count=unit_count,
        field_count=field_count,
    )


@router.get("/analytics", response_model=AnalyticsView)
async def analytics(session: SessionDep) -> AnalyticsView:
    """Corpus totals and per-stage pipeline throughput, over everything.

    Deliberately unpaginated and unfiltered: the screen that reads this
    answers "how big is the corpus and where is the pipeline stuck", and
    both questions are about the whole of it. Three aggregate queries, no
    per-document work -- see `DocumentRepository.corpus_totals` and
    `RegistryRepository.stage_throughput` for why each is one round trip.

    An empty corpus is a 200 with zeros and an empty `stages` list, not a
    404: "no documents yet" is the answer to the question, not an absence of
    the resource, and the UI renders it as an empty state.
    """
    totals = await DocumentRepository(session).corpus_totals()
    registry = RegistryRepository(session)
    stages = await registry.stage_throughput()
    outcomes = await registry.document_outcome_counts()
    return AnalyticsView(
        corpus=CorpusTotalsView(
            document_count=totals["document_count"],
            doc_key_count=totals["doc_key_count"],
            total_bytes=totals["total_bytes"],
            latest_ingested_at=totals["latest_ingested_at"],
        ),
        stages=[
            StageThroughputView(
                name=s["name"],
                done=s["done"],
                running=s["running"],
                queued=s["queued"],
                failed=s["failed"],
                median_seconds=s["median_seconds"],
            )
            for s in stages
        ],
        documents_complete=outcomes["complete"],
        documents_failed=outcomes["failed"],
        chunk_count=totals["chunk_count"],
    )
