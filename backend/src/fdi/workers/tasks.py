"""Tasks. THE canonical shape for every stage in SP-2..SP-18.

Five rules, each closing a verified failure:

1. The task is `def`, never `async def`. Celery has no native async support;
   `async def` returns an un-awaited coroutine and reports SUCCESS. Measured on
   Celery 5.6.3: `@app.task` accepts an `async def` WITHOUT complaint and calling
   it hands back `<coroutine object>`. Guarded by tests/unit/test_no_async_celery_tasks.py.
2. The correlation ID is bound in the SYNC wrapper, BEFORE asyncio.run().
   contextvars is copy-in / no-copy-out: bind it inside the loop and the caller
   never sees it.
3. The bind is UNCONDITIONAL and the clear is in a `finally`. Prefork reuses one
   process for up to `worker_max_tasks_per_child` (50) tasks and Celery resets
   NOTHING between them -- `celery/app/trace.py`, `celery/concurrency/prefork.py`
   and `billiard/pool.py` contain no reference to contextvars at all. A wrapper
   that binds only `if correlation_id` therefore lets a job dispatched without one
   inherit the PREVIOUS job's ID and log under it. Measured through Celery's own
   tracer: job A bound 'cid-A', job B ran with no ID and its body read 'cid-A'.
   Minting when absent keeps every line attributable; clearing keeps this task's
   ID from becoming the next one's default.
4. The engine is created INSIDE the coroutine and disposed in `finally`. A pool
   built at import is inherited across fork() and corrupts silently.
5. The failure handler NEVER masks the cause. `mark_failed` can raise (the job id
   may not exist; the DB may be the thing that broke), and an exception escaping
   an `except` block REPLACES the one being handled. Celery then records the
   bookkeeping failure as the task's error -- and with `result_serializer="json"`
   the original is gone for good: `__context__` is not serialised, so a client's
   `result.get()` re-raises only the mask. Bookkeeping is best-effort; the
   diagnosis is not.

Config is read LAZILY (on the right-hand side of each `or`), so a caller that
injects every dependency reads no Settings at all. Note this is the only place
that is true: `fdi.workers.app` builds Settings at import to construct the Celery
app, so a worker with broken config still fails at import, not here.
"""

import asyncio
import uuid
from typing import Any

import structlog
from minio import Minio

from fdi.pipeline import stages as _stages  # noqa: F401 - registers built-in stages
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.platform.config import Settings, get_settings
from fdi.platform.db.engine import make_engine, make_session_factory
from fdi.platform.db.repository import JobRepository
from fdi.platform.obs.logging import (
    bind_correlation_id,
    clear_correlation_id,
    new_correlation_id,
)
from fdi.platform.storage.artifact_store import ArtifactStore
from fdi.workers.app import app

log = structlog.get_logger(__name__)

PING_PAYLOAD: dict[str, Any] = {"kind": "ping", "schema_version": 1}


def _make_client(settings: Settings) -> Minio:
    return Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )


# `type: ignore[untyped-decorator]` is NOT optional here, and every stage in
# SP-2..SP-18 will need it. Celery 5.6.3 ships no `py.typed` and no stubs are
# installed, so with `ignore_missing_imports = true` the whole package resolves to
# Any -- which makes `app.task(...)` an UNTYPED decorator, and `--strict` implies
# `disallow_untyped_decorators`. Without this, `mypy --strict src` fails:
#
#   error: Untyped decorator makes function "ping_job" untyped  [untyped-decorator]
#
# Scoped to this line on purpose: a module-wide `disallow_untyped_decorators =
# false` override would also wave through a genuinely untyped decorator added
# later. `--strict` also implies `warn_unused_ignores`, so if `celery-types` is
# ever added this comment fails the gate rather than rotting silently.
@app.task(name="fdi.ping_job", bind=False)  # type: ignore[untyped-decorator]
def ping_job(job_id: str, correlation_id: str = "") -> str:
    """Sync wrapper. Binds the correlation ID BEFORE the loop, then runs once.

    Unconditional bind + `finally: clear` -- see rule 3. `if correlation_id:`
    would be a correlation ID LEAK, not an optimisation: this process has already
    run other jobs and their IDs are still bound.
    """
    bind_correlation_id(correlation_id or new_correlation_id())
    try:
        return asyncio.run(_ping_job(job_id))
    finally:
        clear_correlation_id()


async def _ping_job(
    job_id: str,
    *,
    store_bucket: str | None = None,
    client: Minio | None = None,
    db_url: str | None = None,
) -> str:
    """All of this task's I/O, in one coroutine, on one event loop.

    The keyword args exist so tests can inject container-backed dependencies;
    production passes none and reads Settings. `get_settings()` sits on the right
    of each `or` so that an injected caller never reads the environment -- it is
    lru_cached, so the repeats cost a dict lookup.
    """
    engine = make_engine(db_url or get_settings().database_url)
    try:
        session_factory = make_session_factory(engine)
        store = ArtifactStore(
            client or _make_client(get_settings()),
            bucket=store_bucket or get_settings().s3_bucket,
        )

        async with session_factory() as session:
            await JobRepository(session).mark_running(job_id)
            await session.commit()
        log.info("ping_job.running", job_id=job_id)

        await store.ensure_bucket()
        # NOTE: the body carries no job id and no timestamp — Global Constraint 6.
        key = await store.put_json(f"jobs/{job_id}/ping.json", PING_PAYLOAD)

        async with session_factory() as session:
            await JobRepository(session).mark_done(job_id, artifact_key=key)
            await session.commit()
        log.info("ping_job.done", job_id=job_id, artifact_key=key)
        return key
    except Exception as exc:
        # Log the CAUSE first: everything after this point can fail, and if it
        # does, this line is the only surviving record of why the job died.
        log.exception("ping_job.failed", job_id=job_id)
        try:
            # A fresh factory, not `session_factory`: that name is bound inside the
            # `try` and would be unbound here had its own line raised.
            async with make_session_factory(engine)() as session:
                await JobRepository(session).mark_failed(job_id, error=repr(exc))
                await session.commit()
        except Exception:
            # Swallowed ON PURPOSE -- see rule 5. Letting this escape would replace
            # `exc` with whatever broke while recording it, and Celery would store
            # the mask as the task's error. Logged, never lost.
            log.exception("ping_job.mark_failed_failed", job_id=job_id)
        # Bare `raise` re-raises `exc`, NOT the swallowed one: the inner handler has
        # exited, so the exception being handled is the outer one again. Verified.
        raise
    finally:
        await engine.dispose()


async def _run_stage_async(
    doc_id: str,
    stage_name: str,
    run_id: str,
    correlation_id: str,
    *,
    db_url: str | None = None,
    client: Minio | None = None,
    store_bucket: str | None = None,
) -> list[str]:
    """The task's I/O on one event loop. Keyword args let tests inject containers;
    production passes none and reads Settings (mirrors _ping_job)."""
    engine = make_engine(db_url or get_settings().database_url)
    try:
        session_factory = make_session_factory(engine)
        store = ArtifactStore(
            client or _make_client(get_settings()),
            bucket=store_bucket or get_settings().s3_bucket,
        )
        await store.ensure_bucket()
        resolver = DagResolver(REGISTRY.all())
        return await run_stage(
            doc_id=doc_id, stage_name=stage_name, run_id=run_id,
            correlation_id=correlation_id, session_factory=session_factory,
            store=store, registry=REGISTRY, resolver=resolver,
        )
    finally:
        await engine.dispose()


@app.task(name="fdi.run_stage", bind=False)  # type: ignore[untyped-decorator]
def run_stage_task(doc_id: str, stage_name: str, correlation_id: str = "") -> None:
    """Sync wrapper. Binds correlation BEFORE asyncio.run, clears in finally
    (unconditional bind -- a prefork process reuses across jobs). Enqueues each
    successor the DAG returns."""
    corr = correlation_id or new_correlation_id()
    bind_correlation_id(corr)
    try:
        run_id = str(uuid.uuid4())
        successors = asyncio.run(_run_stage_async(doc_id, stage_name, run_id, corr))
        for nxt in successors:
            run_stage_task.apply_async(args=[doc_id, nxt], kwargs={"correlation_id": corr})
    finally:
        clear_correlation_id()
