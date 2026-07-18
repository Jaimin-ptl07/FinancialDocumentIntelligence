"""HTTP surface."""

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import cast

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
    AskRequest,
    AskResponse,
    ConceptCompareRequest,
    ConceptComparisonView,
    ConceptLookupView,
    DocumentStructureView,
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
    IndexFtsView,
    JobAccepted,
    JobView,
    KeywordHitView,
    KeywordSearchRequest,
    KeywordSearchResultsView,
    LeaderboardEntry,
    LeaderboardView,
    OcrTriggerView,
    ReadingOrderView,
    ReadyView,
    ReprocessView,
    SearchRequest,
    SearchResultsView,
    SearchResultView,
    SemanticTriggerView,
    StageStatusView,
    StagesView,
    StaleView,
    TablesView,
    VectorSearchRequest,
    VectorSearchResultsView,
    VectorSearchResultView,
)
from fdi.core.extractor import parse_field_specs
from fdi.core.ontology import CanonicalConcept
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
from fdi.pipeline.intake import intake_document
from fdi.pipeline.retrieval import (
    hybrid_search,
    index_chunks_fts,
    keyword_search,
    vector_search,
)
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
        store=store,
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
# RT-3: POST /documents/{id}/index_fts + POST /retrieve/keyword
# --------------------------------------------------------------------------


@router.post("/documents/{doc_id}/index_fts", response_model=IndexFtsView)
async def trigger_index_fts(doc_id: str, session: SessionDep, store: StoreDep) -> IndexFtsView:
    """Synchronous, task-free -- like /search and /entities/resolve, NOT like
    ocr/semantic/embedding/graph_load. Reads the already-cached `chunks`
    artifact from MinIO and upserts it into Postgres; no external API call
    (no Azure, no Milvus, no Neo4j), so there is nothing to queue.
    """
    if await RegistryRepository(session).get_document(doc_id) is None:
        raise HTTPException(status_code=404, detail=f"document {doc_id} not found")
    count = await index_chunks_fts(session=session, store=store, doc_id=doc_id)
    await session.commit()
    return IndexFtsView(doc_id=doc_id, chunks_indexed=count)


@router.post("/retrieve/keyword", response_model=KeywordSearchResultsView)
async def keyword_search_route(
    body: KeywordSearchRequest, session: SessionDep
) -> KeywordSearchResultsView:
    hits = await keyword_search(
        session=session, query=body.query, k=body.k,
        section_type=body.section_type, doc_key=body.doc_key, version=body.version,
    )
    return KeywordSearchResultsView(results=[KeywordHitView(**h) for h in hits])


# --------------------------------------------------------------------------
# RT-4: POST /retrieve -- hybrid (RRF) retrieval over RT-2's vector_search +
# RT-3's keyword_search. Unlike /retrieve/vector, this route has NO
# 503-when-unconfigured branch: keyword_search has no external dependency
# (Postgres is mandatory config, RT-3 sec 2 decision 9), so hybrid_search
# degrades to keyword-only when embedder/vector_store are unconfigured
# rather than failing the whole request -- design spec sec 2 decision 2.
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
        store=store,
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


def _read_schema_file(schema_path: str) -> object:
    """Blocking file read, run off the event loop via asyncio.to_thread by
    the caller -- same posture as ArtifactStore's sync-minio-SDK calls."""
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


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

    schema_raw = await asyncio.to_thread(_read_schema_file, schema_path)
    schema = schema_raw if isinstance(schema_raw, dict) else {}
    fields = parse_field_specs(schema, body.module)

    ctx = ExtractContext(
        session=session, store=store, embedder=embedder, vector_store=vector_store,
        doc_id=doc.id, doc_key=body.doc_key, version=version,
    )
    result = await extract_document(ctx=ctx, fields=fields, client=extractor_client)
    return ExtractDocumentView.model_validate(
        {"doc_key": body.doc_key, "module": body.module, **result}
    )
