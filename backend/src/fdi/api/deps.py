"""Request-scoped dependencies. The engine is owned by lifespan, never by a module."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdi.pipeline.dispatch import StageDispatcher
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.evaluation import CandidateRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.obs.logging import get_correlation_id as _get_bound_correlation_id
from fdi.platform.storage.artifact_store import ArtifactStore


def get_correlation_id() -> str:
    """The id `CorrelationIdMiddleware` bound for this request.

    Raises rather than returning `""`. `get_correlation_id()` returns the empty
    string when unbound, never None -- so the obvious call site writes a `str`
    that satisfies the repository's signature, satisfies mypy, and means nothing,
    and `ix_jobs_correlation_id` collects one entry shared by every context-less
    job. There are three ways to spell "absent" (NULL, "", a real id) and only
    one of them any type checker objects to.

    An empty id here is a wiring bug -- the middleware is missing -- so it fails
    loudly as a 500 with a traceback naming this function, instead of quietly
    poisoning a column that a later index scan will have to be trusted.
    """
    correlation_id = _get_bound_correlation_id()
    if not correlation_id:
        raise RuntimeError(
            "No correlation id bound for this request. CorrelationIdMiddleware "
            "must be installed on the app -- see fdi.api.app.create_app."
        )
    return correlation_id


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = request.app.state.session_factory
    async with factory() as session:
        yield session


def get_store(request: Request) -> ArtifactStore:
    store: ArtifactStore = request.app.state.artifact_store
    return store


def get_minio(request: Request) -> Minio:
    client: Minio = request.app.state.minio
    return client


def get_dispatcher(request: Request) -> StageDispatcher:
    """The dispatcher built in lifespan, which is the only thing that knows the task.

    `api` may not import `fdi.workers`, so the route cannot reach the task itself.
    It asks for a StageDispatcher and stays ignorant of what is inside it -- which
    is also what lets the route be tested without a broker.
    """
    dispatcher: StageDispatcher = request.app.state.dispatcher
    return dispatcher


def get_resolver(request: Request) -> DagResolver:
    """The resolver built in lifespan from `REGISTRY.all()`.

    Built once, in lifespan, rather than per-request: the registry is
    populated once (when `fdi.workers.tasks` is imported) and does not
    change over the process lifetime, so re-resolving the topology on every
    request would be pure waste.
    """
    resolver: DagResolver = request.app.state.resolver
    return resolver


def get_session_factory(request: Request) -> "async_sessionmaker[AsyncSession]":
    """The same factory `get_session` opens requests off of, exposed directly.

    `run_evaluation` needs a `session_factory` rather than a single session
    because it opens its own `async with session_factory()` block to scope
    the eval-run's commit -- a route-scoped `SessionDep` would tie that
    transaction to the request's lifetime instead.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory


def get_candidates(request: Request) -> CandidateRegistry:
    """The registry lifespan populated by importing `fdi.pipeline.candidates`."""
    candidates: CandidateRegistry = request.app.state.candidates
    return candidates


def get_embedder(request: Request) -> EmbeddingClient | None:
    embedder: EmbeddingClient | None = request.app.state.embedder
    return embedder


def get_vector_store(request: Request) -> VectorStore | None:
    vector_store: VectorStore | None = request.app.state.vector_store
    return vector_store


SessionDep = Annotated[AsyncSession, Depends(get_session)]
StoreDep = Annotated[ArtifactStore, Depends(get_store)]
CorrelationIdDep = Annotated[str, Depends(get_correlation_id)]
DispatcherDep = Annotated[StageDispatcher, Depends(get_dispatcher)]
ResolverDep = Annotated[DagResolver, Depends(get_resolver)]
SessionFactoryDep = Annotated["async_sessionmaker[AsyncSession]", Depends(get_session_factory)]
CandidatesDep = Annotated[CandidateRegistry, Depends(get_candidates)]
EmbedderDep = Annotated[EmbeddingClient | None, Depends(get_embedder)]
VectorStoreDep = Annotated[VectorStore | None, Depends(get_vector_store)]