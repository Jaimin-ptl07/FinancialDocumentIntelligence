"""Request-scoped dependencies. The engine is owned by lifespan, never by a module."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.pipeline.dispatch import StageDispatcher
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


SessionDep = Annotated[AsyncSession, Depends(get_session)]
StoreDep = Annotated[ArtifactStore, Depends(get_store)]
CorrelationIdDep = Annotated[str, Depends(get_correlation_id)]
DispatcherDep = Annotated[StageDispatcher, Depends(get_dispatcher)]