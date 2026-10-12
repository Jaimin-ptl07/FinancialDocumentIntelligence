"""FastAPI application factory."""

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from minio import Minio
from pydantic import ValidationError
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from fdi.api.auth import auth_router, require_access_token
from fdi.api.errors import install_error_handlers
from fdi.api.middleware import CorrelationIdMiddleware
from fdi.api.routes import public_router, router
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.resolver import DagResolver
from fdi.platform.agent import AzureOpenAIAgentClient
from fdi.platform.config import get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.embeddings import BgeEmbeddingClient
from fdi.platform.extractor import AzureOpenAIExtractorClient
from fdi.platform.graphstore import Neo4jGraphStore
from fdi.platform.obs.logging import configure_logging
from fdi.platform.obs.sentry import init_sentry
from fdi.platform.storage.artifact_store import ArtifactStore, build_presign_client
from fdi.platform.vectorstore import MilvusVectorStore

log = structlog.get_logger(__name__)


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
    # A SECOND client, used ONLY to sign download URLs for the browser's view of
    # the store (see build_presign_client).
    presign_client = build_presign_client(settings)

    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.minio = client
    app.state.artifact_store = ArtifactStore(
        client, bucket=settings.s3_bucket, presign_client=presign_client
    )
    # Bucket creation was previously lazy -- only /health's s3 probe or a
    # worker's first task happened to call ensure_bucket(). On a fresh
    # environment (bucket never created), any OTHER route hit first raised
    # minio.error.S3Error: NoSuchBucket instead of the app just working.
    # Doing it here means the bucket exists before the app can serve traffic.
    #
    # But an UNREACHABLE object store must not keep the app from BOOTING: the
    # process can come up degraded and let /ready say so -- the same
    # restart-loop reasoning as /health-vs-/ready (a backend outage is not a
    # reason to kill or refuse to start the API). Warning, not exception, and
    # the same suppress-if-logging-itself-breaks idiom as /ready's _probe.
    # When the store IS reachable this changes nothing: the bucket is ensured
    # exactly as before.
    try:
        await app.state.artifact_store.ensure_bucket()
    except Exception as exc:
        with contextlib.suppress(Exception):
            log.warning(
                "startup.bucket_ensure_failed",
                bucket=settings.s3_bucket,
                error=repr(exc),
            )
    app.state.embedder = BgeEmbeddingClient.from_settings(settings)
    app.state.vector_store = MilvusVectorStore.from_settings(settings)
    app.state.graph_store = Neo4jGraphStore.from_settings(settings)
    app.state.entity_threshold = settings.entity_sim_threshold
    app.state.agent_client = AzureOpenAIAgentClient.from_settings(settings)
    app.state.extractor_client = AzureOpenAIExtractorClient.from_settings(settings)
    app.state.extraction_schema_path = settings.extraction_schema_path

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
    from fdi.pipeline.jobs import JobDispatcher
    from fdi.workers.tasks import run_job_task, run_stage_task

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
    # T5: the expensive routes publish API jobs. Same Celery app as the stage
    # task, so the broker re-assertion above covers it too.
    app.state.job_dispatcher = JobDispatcher(run_job_task)

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


# The historical hardcoded value, now the FALLBACK rather than the policy: the
# Next.js dev server (SP-1/SP-18), which fetches this API cross-origin. Without
# CORS the browser lets the request go out but forbids the JS from READING the
# response -- even a simple GET -- so the UI's job view never populates and
# SP-1 done-criterion 6 silently fails, while curl/httpx (which enforce no
# origin policy) pass. That gap is exactly why the e2e httpx test cannot catch
# this and a real browser must.
_DEFAULT_CORS_ORIGINS = ["http://localhost:3000"]


def _cors_origins() -> list[str]:
    """`FDI_CORS_ORIGINS`, or the dev default when Settings cannot be built.

    The guard is structural, not defensive padding. `app = create_app()` at
    the bottom of this module means create_app runs at IMPORT time -- during
    pytest COLLECTION, where tests/conftest.py::_isolate_fdi_env has
    deliberately cleared every FDI_ variable so a developer's stray FDI_FOO=1
    cannot fail unrelated tests. An unguarded get_settings() here would raise
    `6 validation errors for Settings` and take the whole suite down with it.

    Narrow on purpose: ValidationError is the ONE failure an unconfigured
    environment produces (the required-field errors, and the unknown-FDI_-var
    ValueError that the model_validator raises and pydantic wraps). Anything
    else is a real bug and must still propagate.
    """
    try:
        return list(get_settings().cors_origins)
    except ValidationError:
        return list(_DEFAULT_CORS_ORIGINS)


def _sentry_dsn() -> str | None:
    """`FDI_SENTRY_DSN`, or None when Settings cannot be built.

    Same narrow guard as `_cors_origins`, same reason: create_app runs at
    import time under pytest collection with every FDI_ variable cleared, and
    monitoring must fall back to "off", never take collection down.
    """
    try:
        return get_settings().sentry_dsn
    except ValidationError:
        return None


def create_app() -> FastAPI:
    # Before the app is built: the Starlette integration instruments
    # Starlette.__call__, and a DSN-less deployment must initialise nothing at
    # all (obs/sentry.py's no-op contract).
    init_sentry(_sentry_dsn(), FastApiIntegration(), StarletteIntegration())
    # docs_url/redoc_url/openapi_url None: FastAPI mounts these on the APP,
    # not either router, so the auth gate below never covers them -- and an
    # anonymous /openapi.json is a complete inventory of every protected
    # route with request schemas, i.e. exactly the reconnaissance the gate
    # exists to prevent. Disabled outright rather than moved behind the gate:
    # the one thing nobody needs Swagger UI to read is the route table. The
    # HTTP suite is this API's contract; there is no Swagger to lose.
    app = FastAPI(
        title="Financial Document Intelligence",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
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
        allow_origins=_cors_origins(),
        # The dev UI calls /auth/* cross-origin with credentials so the
        # httpOnly refresh cookie can be set and sent (T4). Safe only because
        # the origins are an explicit allowlist -- never "*".
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Public by design (ADR-0002): the three /auth session routes, plus /health
    # and /ready for infrastructure probes. Everything else -- the `router`
    # include below -- requires a valid access token; the dependency rides the
    # include, not each route, so a route added to `router` later is gated by
    # default and has to opt OUT (public_router) to be reachable anonymously.
    app.include_router(auth_router)
    app.include_router(public_router)
    app.include_router(router, dependencies=[Depends(require_access_token)])
    return app


app = create_app()
