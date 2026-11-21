"""API jobs (T5): the four expensive routes, run off the request.

`/ask`, `/extract/document`, `/factsheet/document` and `/eval` each made many
sequential Azure calls inside one HTTP request -- minutes long, no timeout, and
dead the moment a proxy or browser gave up. Now the route validates what it
can answer instantly (is the client configured? does the document exist and
has it finished indexing? does the candidate exist?), writes an `api_job` row,
and publishes `fdi.run_job`. The worker calls `execute_job`, which walks the
row queued -> running -> succeeded | failed and stores the body the route used
to return inline.

Layering mirrors stage dispatch exactly (see `fdi.pipeline.dispatch`): the
API publishes through `JobDispatcher` and never names the Celery task; the
worker builds `JobServices` from Settings and the tests build it from fakes.

Failure contract: a job that cannot be answered (an unconfigured client on the
worker, a document deleted after submit, an unknown candidate) FAILS with a
readable error -- never raises out of `execute_job`. An unexpected exception
also marks the job failed, then re-raises, so Celery and Sentry still see the
crash with its traceback; recording the failure is best-effort and never masks
the cause (workers/tasks.py rule 5).
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast, get_args

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdi.core.extractor import parse_field_specs
from fdi.core.factsheet import document_factsheet
from fdi.core.xbrl import calculation_arcs, export_instance
from fdi.pipeline.agent import AgentClient
from fdi.pipeline.agent_tools import AgentContext
from fdi.pipeline.ask import answer_question
from fdi.pipeline.dispatch import TaskSender
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.eval_service import run_evaluation
from fdi.pipeline.evaluation import CandidateRegistry
from fdi.pipeline.extractor import ExtractContext, ExtractorClient, extract_document
from fdi.pipeline.graphstore import GraphStore
from fdi.pipeline.schema_store import load_schema
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.models import Document
from fdi.platform.db.repository import DocumentRepository, JobRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore

log = structlog.get_logger(__name__)

# The one spelling of the kind vocabulary on the Python side (the api_job
# CHECK constraint is the database's). `_HANDLERS` is keyed by it and the
# jobs listing validates `?kind=` against it.
JobKind = Literal["ask", "extract", "factsheet", "eval"]
JOB_KINDS: tuple[str, ...] = get_args(JobKind)
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


class JobRejected(Exception):
    """The job cannot be answered. Its message is what the caller reads: the
    route turns it into a 4xx before a job exists, the runner into the failed
    job's `error` after."""


class DocumentNotFound(JobRejected):
    pass


class DocumentNotIndexed(JobRejected):
    pass


class JobDispatcher:
    """Publishes an API job. Same seam as `StageDispatcher`: the composition
    root injects the Celery task, routes only ever see this."""

    def __init__(self, job_task: TaskSender) -> None:
        self._job_task = job_task

    def dispatch(self, job_id: str, correlation_id: str) -> None:
        self._job_task.apply_async(args=[job_id], kwargs={"correlation_id": correlation_id})


@dataclass(frozen=True)
class JobServices:
    """Everything a job may need. Clients are optional exactly as they are on
    the API: an unconfigured one fails the job that needs it, nothing else."""

    session_factory: "async_sessionmaker[AsyncSession]"
    store: ArtifactStore
    embedder: EmbeddingClient | None
    vector_store: VectorStore | None
    graph_store: GraphStore | None
    agent_client: AgentClient | None
    extractor_client: ExtractorClient | None
    entity_threshold: float
    extraction_schema_path: str
    candidates: CandidateRegistry


async def indexed_document(session: AsyncSession, doc_key: str) -> Document:
    """The latest version of `doc_key`, provided its pipeline reached `index`.

    Until `index` is done, Milvus holds nothing searchable for the document,
    so extraction would quietly answer null for every field -- refusing is the
    only honest response. Shared by the submit-time check and the runner (a
    document can be deleted, or reprocessed, between the two).
    """
    versions = await DocumentRepository(session).list_by_doc_key(doc_key)
    if not versions:
        raise DocumentNotFound(f"no documents for doc_key {doc_key!r}")
    doc = versions[-1]  # ordered by version ascending -> last is latest
    if doc.version is None:
        # Intake always sets a version; a NULL here is corrupt state, not a 4xx.
        raise RuntimeError(f"document {doc.id} has no version")
    index_run = await RegistryRepository(session).latest_run(doc.id, "index")
    if index_run is None or index_run.status != "done":
        raise DocumentNotIndexed(
            "this document's pipeline has not finished indexing yet -- "
            "retrieval would find nothing. Wait for all stages to complete, "
            "then try again"
        )
    return doc


async def execute_job(job_id: str, services: JobServices) -> None:
    """Run one job to a terminal status.

    At-least-once, like every task here (`task_acks_late`): a job already
    succeeded or failed is left alone, but one a lost worker left `running`
    is run again in full when its message is redelivered -- re-spending its
    LLM calls is the price of never silently dropping a job."""
    async with services.session_factory() as s:
        repo = JobRepository(s)
        job = await repo.get(job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found")
        if job.status in TERMINAL_STATUSES:
            log.info("job.already_terminal", job_id=job_id, status=job.status)
            return
        kind, params, correlation_id = job.kind, dict(job.params), job.correlation_id
        await repo.mark_running(job_id)
        await s.commit()

    log.info("job.running", job_id=job_id, kind=kind)
    try:
        result = await _HANDLERS[cast(JobKind, kind)](params, services, correlation_id)
    except JobRejected as exc:
        await _record_failure(services, job_id, str(exc))
        return
    except Exception as exc:
        await _record_failure(services, job_id, f"{type(exc).__name__}: {exc}")
        raise

    try:
        async with services.session_factory() as s:
            await JobRepository(s).mark_succeeded(job_id, result)
            await s.commit()
    except Exception as exc:
        # An unstorable result (not JSON-serialisable) or a DB blip here would
        # otherwise strand the row in `running` with the work already paid for.
        await _record_failure(
            services, job_id, f"result could not be stored: {type(exc).__name__}: {exc}"
        )
        raise
    log.info("job.succeeded", job_id=job_id, kind=kind)


async def _record_failure(services: JobServices, job_id: str, error: str) -> None:
    log.warning("job.failed", job_id=job_id, error=error)
    try:
        async with services.session_factory() as s:
            await JobRepository(s).mark_failed(job_id, error)
            await s.commit()
    except Exception as exc:  # noqa: BLE001 -- bookkeeping must not mask the cause
        log.error("job.mark_failed_failed", job_id=job_id, error=repr(exc))


# --------------------------------------------------------------------------
# Handlers. Each takes the params its route accepted and returns the body the
# route used to return inline (JSON-serialisable).
# --------------------------------------------------------------------------


async def _ask(params: dict[str, object], services: JobServices, correlation_id: str) -> object:
    if services.agent_client is None:
        raise JobRejected("ask is not configured: missing agent_client")
    max_rounds = params.get("max_rounds")
    async with services.session_factory() as s:
        ctx = AgentContext(
            session=s, store=services.store, embedder=services.embedder,
            vector_store=services.vector_store, graph_store=services.graph_store,
            default_entity_threshold=services.entity_threshold,
            correlation_id=correlation_id,
        )
        result = await answer_question(
            question=str(params["question"]), ctx=ctx, agent_client=services.agent_client,
            max_rounds=max_rounds if isinstance(max_rounds, int) else 5,
        )
        # answer_question records llm_usage_event rows and only flushes.
        await s.commit()
    return result


async def _extract_records(
    params: dict[str, object],
    services: JobServices,
    correlation_id: str,
    *,
    whole_schema: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    """(the schema, extract_document's result) -- shared by extract and factsheet."""
    if services.extractor_client is None:
        raise JobRejected("extraction is not configured: missing extractor_client")
    doc_key = str(params["doc_key"])
    module = params.get("module")
    async with services.session_factory() as s:
        doc = await indexed_document(s, doc_key)
        assert doc.version is not None  # indexed_document guarantees it
        schema, _source = await load_schema(
            store=services.store, seed_path=services.extraction_schema_path
        )
        fields = parse_field_specs(
            schema, None if whole_schema or not isinstance(module, str) else module
        )
        ctx = ExtractContext(
            session=s, store=services.store, embedder=services.embedder,
            vector_store=services.vector_store, doc_id=doc.id, doc_key=doc_key,
            version=doc.version, correlation_id=correlation_id,
        )
        result = await extract_document(
            ctx=ctx, fields=fields, client=services.extractor_client,
            force=bool(params.get("force", False)),
        )
        # usage rows only flush; the extraction record itself is in the store.
        await s.commit()
    return schema, result


async def _extract(
    params: dict[str, object], services: JobServices, correlation_id: str
) -> object:
    _schema, result = await _extract_records(
        params, services, correlation_id, whole_schema=False
    )
    return {"doc_key": params["doc_key"], "module": params.get("module"), **result}


async def _factsheet(
    params: dict[str, object], services: JobServices, correlation_id: str
) -> object:
    schema, result = await _extract_records(params, services, correlation_id, whole_schema=True)
    records = result.get("fields")
    bundle = document_factsheet(schema, records if isinstance(records, list) else [])
    doc_key = str(params["doc_key"])
    if params.get("format") == "xbrl":
        facts = bundle.get("facts")
        return export_instance(
            facts if isinstance(facts, dict) else {}, entity=f"urn:fdi:{doc_key}"
        )
    derived = bundle.get("derived")
    bundle["calculation_arcs"] = calculation_arcs(derived if isinstance(derived, dict) else {})
    bundle["doc_key"] = doc_key
    return bundle


async def _eval(params: dict[str, object], services: JobServices, correlation_id: str) -> object:
    candidate = str(params["candidate"])
    if candidate not in services.candidates.all():
        raise JobRejected(f"candidate {candidate!r} not found")
    dataset_version = params["dataset_version"]
    if not isinstance(dataset_version, int):
        raise JobRejected(f"dataset_version must be an integer, got {dataset_version!r}")
    run_id = str(uuid.uuid4())
    aggregate = await run_evaluation(
        run_id=run_id, candidate_name=candidate, dataset_name=str(params["dataset"]),
        dataset_version=dataset_version, session_factory=services.session_factory,
        store=services.store, candidates=services.candidates,
    )
    return {"run_id": run_id, "aggregate": aggregate}


_HANDLERS: dict[
    JobKind, Callable[[dict[str, object], JobServices, str], Awaitable[object]]
] = {"ask": _ask, "extract": _extract, "factsheet": _factsheet, "eval": _eval}
