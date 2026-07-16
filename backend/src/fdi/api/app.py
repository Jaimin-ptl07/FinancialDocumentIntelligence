"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from minio import Minio

from fdi.api.errors import install_error_handlers
from fdi.api.middleware import CorrelationIdMiddleware
from fdi.api.routes import router
from fdi.platform.config import get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.obs.logging import configure_logging
from fdi.platform.storage.artifact_store import ArtifactStore


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Owns the engine. Created here — inside the running loop — not at import."""
    settings = get_settings()
    # FIRST, before anything else in this function can log. An unconfigured
    # structlog renders through rich, which is unencodable on cp1252 stdout, and
    # that UnicodeEncodeError escapes whatever was logging -- the failure mode
    # that made Task 15's 500 handler return a bodyless response. get_settings()
    # above cannot log; it can only raise, and a config error must reach the
    # console however logging is set up.
    configure_logging(settings.log_level)

    engine = make_engine(settings.database_url)
    client = Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )

    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.minio = client
    app.state.artifact_store = ArtifactStore(client, bucket=settings.s3_bucket)

    # THE one place `api` may name `fdi.workers`, and the reason it is legal here
    # is that this function is the composition root: it is where concrete things
    # are chosen and handed to abstractions, and nothing imports it back. Routes
    # take a JobDispatcher; only this line knows which task is inside it.
    #
    # Deliberately a local import, not a module-level one. At module level it
    # would import the Celery app -- which builds Settings in its own module body
    # -- at `import fdi.api.app` time, i.e. during test COLLECTION, before any
    # fixture has set the environment.
    #
    # `.importlinter` exempts exactly `fdi.api.app -> fdi.workers.tasks`. It does
    # NOT make `api -> workers` legal: putting this import back in routes.py
    # breaks the contract again, which was re-run and confirmed rather than
    # inferred from the config syntax.
    from fdi.pipeline.dispatch import JobDispatcher
    from fdi.workers.tasks import ping_job

    app.state.dispatcher = JobDispatcher(ping_job)
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Financial Document Intelligence", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    # Must sit INSIDE ServerErrorMiddleware (which is always outermost) so that
    # the id is still bound when `errors._unhandled` renders a 500 and reaches
    # for `instance`. add_middleware() prepends, so this is exactly where a
    # user middleware goes; there is no arrangement that puts it outside.
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(router)
    return app


app = create_app()