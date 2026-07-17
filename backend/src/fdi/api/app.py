"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from minio import Minio

from fdi.api.errors import install_error_handlers
from fdi.api.middleware import CorrelationIdMiddleware
from fdi.api.routes import router
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.resolver import DagResolver
from fdi.platform.config import get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.embeddings import AzureOpenAIEmbeddingClient
from fdi.platform.obs.logging import configure_logging
from fdi.platform.storage.artifact_store import ArtifactStore
from fdi.platform.vectorstore import MilvusVectorStore


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
    # A SECOND client, built for the host-reachable endpoint, used ONLY to sign
    # download URLs. presigned_get_object embeds whatever endpoint its client was
    # constructed with, and `client` above holds the in-network `minio:9000` -- a name
    # a host browser cannot resolve, so the artifact_url a UI needs would 404. Falls
    # back to s3_endpoint when s3_public_endpoint is unset (single-host prod / tests),
    # so this is identical to `client` there and changes nothing.
    #
    # region is REQUIRED here, not cosmetic: presigned_get_object first calls
    # _get_region, which -- absent a constructor region -- issues a live
    # GetBucketLocation to the client's endpoint. This client's endpoint is the
    # host's view (localhost:9000), which is deliberately NOT reachable from inside
    # this container, so that lookup would ConnectionRefused and turn every
    # /jobs/{id} with an artifact into a 500. Setting region makes _get_region return
    # it offline (verified in minio/api.py), so signing touches no network. us-east-1
    # is MinIO's default and MUST match the server's region or the signature is
    # rejected; a non-MinIO S3 behind a public endpoint in another region would need
    # this made configurable.
    presign_client = Minio(
        settings.s3_public_endpoint or settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
        region="us-east-1",
    )

    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.minio = client
    app.state.artifact_store = ArtifactStore(
        client, bucket=settings.s3_bucket, presign_client=presign_client
    )
    app.state.embedder = AzureOpenAIEmbeddingClient.from_settings(settings)
    app.state.vector_store = MilvusVectorStore.from_settings(settings)

    # THE one place `api` may name `fdi.workers`, and the reason it is legal here
    # is that this function is the composition root: it is where concrete things
    # are chosen and handed to abstractions, and nothing imports it back. Routes
    # take a StageDispatcher; only this line knows which task is inside it.
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
    #
    # Importing `run_stage_task` also imports `fdi.pipeline.stages` (tasks.py does
    # `from fdi.pipeline import stages as _stages`), which is what registers the
    # built-in stages into `REGISTRY` for THIS process -- the API never resolves a
    # DAG itself, but the resolver's registry must still be populated for anything
    # downstream that inspects it.
    from fdi.pipeline.dispatch import StageDispatcher
    from fdi.workers.tasks import run_stage_task

    # REGISTRY is populated as a side effect of the import above (tasks.py does
    # `from fdi.pipeline import stages as _stages`), so it must be built AFTER
    # that import, not before.
    app.state.resolver = DagResolver(REGISTRY.all())

    # Re-assert the broker from THIS process's Settings, rather than inheriting
    # whatever was in os.environ the first time fdi.workers.app was imported.
    #
    # workers/app.py runs `app = create_celery_app()` in its module body, so its
    # broker_url is frozen at first import, anywhere in the process, forever --
    # measured: a later os.environ change plus get_settings.cache_clear() does NOT
    # move it. In production that is invisible (env is set before uvicorn starts).
    # Under test it made the suite depend on FILESYSTEM SORT ORDER: it passed only
    # because `tests/integration` collects before `tests/unit`, so the Celery app
    # happened to be built with the container's broker first. Forcing the reverse
    # order hangs -- /ping-job retries against the stub host `h` forever, exit 124.
    #
    # The composition root owns wiring, so it should say which broker it dispatches
    # to instead of depending on import order to be lucky.
    run_stage_task.app.conf.broker_url = settings.broker_url
    run_stage_task.app.conf.result_backend = settings.result_backend

    app.state.dispatcher = StageDispatcher(run_stage_task)

    # `api -> pipeline` is a legal edge in the import contract (only
    # `api -> workers` is the one exemption carved out above), so this import
    # is ordinary, not a composition-root special case. It is still done here,
    # not at module level, so that importing `fdi.api.app` for its FastAPI type
    # alone does not have the side effect of registering candidates.
    #
    # `fdi.pipeline.candidates` is imported for its side effect: importing
    # `uppercase` registers `UppercaseCandidate` into the module-level
    # `CANDIDATES` registry. Without this import the registry stays empty in
    # this process and every `/eval` call 404s with "candidate not found",
    # even for `"uppercase"`.
    import fdi.pipeline.candidates  # noqa: F401
    from fdi.pipeline.evaluation import CANDIDATES

    app.state.candidates = CANDIDATES
    try:
        yield
    finally:
        await engine.dispose()


# The one browser origin this walking skeleton serves: the Next.js dev server
# (Task 22), which fetches this API cross-origin. Without CORS the browser lets
# the request go out but forbids the JS from READING the response -- even a simple
# GET -- so the UI's job view never populates and done-criterion 6 silently fails,
# while curl/httpx (which enforce no origin policy) pass. That gap is exactly why
# the e2e httpx test cannot catch this and a real browser must.
#
# Hardcoded, dev-scoped, like `region="us-east-1"` in the lifespan above: a single
# known origin, not `"*"`. A real deployment serves the UI same-origin (behind one
# proxy) or needs a configurable origin allowlist -- a Settings field carrying its
# own x-backend-env / .env.example / consistency-test ripple, deferred to when
# there is a deployment topology to configure it against.
_DEV_ORIGINS = ["http://localhost:3000"]


def create_app() -> FastAPI:
    app = FastAPI(title="Financial Document Intelligence", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    # Must sit INSIDE ServerErrorMiddleware (which is always outermost) so that
    # the id is still bound when `errors._unhandled` renders a 500 and reaches
    # for `instance`. add_middleware() prepends, so this is exactly where a
    # user middleware goes; there is no arrangement that puts it outside.
    app.add_middleware(CorrelationIdMiddleware)
    # Added last, so it is the OUTERMOST user middleware: a preflight OPTIONS is
    # answered here and short-circuits before it can reach correlation binding or
    # routing, and the ACAO header is attached to every response on the way out,
    # including the ones the correlation middleware wrapped.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEV_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()