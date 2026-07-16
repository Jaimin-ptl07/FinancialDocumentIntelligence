"""`workers.tasks` -- the sync/async seam every stage in SP-2..SP-18 copies.

NOTE THE MARKERS. `pytest.mark.integration` is applied per-test, NOT as a module
level `pytestmark`, because half of what this file proves needs no container:
the correlation-ID discipline and the never-`async def` rule are properties of
the sync WRAPPER, and the wrapper touches no I/O. Marking them `integration`
would hide the single most important assertion in this module behind a Docker
daemon, and the fast loop (`-m "not integration"`) is the loop that actually
runs. They stay in this file because they are claims about THIS module; they are
simply honest about not needing Postgres to make them.

Every test requests `stub_fdi_env` -- even the ones that inject every dependency
-- because `import fdi.workers.tasks` itself reads Settings (it imports
`fdi.workers.app`, whose module body builds the Celery app). Without it, running
any single test in this file alone raises ValidationError at the import line.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.models import JobStatus
from fdi.platform.db.repository import JobRepository
from fdi.platform.obs.logging import get_correlation_id
from fdi.platform.storage.artifact_store import ArtifactStore

BACKEND = Path(__file__).resolve().parents[2]

# Runs in a FRESH interpreter -- see test_ping_job_is_registered_by_worker_boot_discovery.
_DISCOVERY_PROBE = """
from fdi.workers.app import app

assert "fdi.ping_job" not in app.tasks, (
    "importing fdi.workers.app alone registered the task, so this probe cannot "
    "attribute registration to autodiscovery"
)
app.loader.import_default_modules()
print("REGISTERED" if "fdi.ping_job" in app.tasks else "MISSING")
"""


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


async def _new_job(session_factory: async_sessionmaker[AsyncSession], cid: str) -> str:
    job_id = str(uuid.uuid4())
    async with session_factory() as s:
        await JobRepository(s).create(job_id, correlation_id=cid)
        await s.commit()
    return job_id


# --------------------------------------------------------------------------- #
# The wrapper's contract. No container: this is all contextvars and `def`.
# --------------------------------------------------------------------------- #


def test_ping_job_task_is_not_a_coroutine_function(stub_fdi_env: None) -> None:
    """Guarded by the AST test too, but pin the actual object: Celery would
    report SUCCESS on a coroutine while doing nothing.

    `.run` is load-bearing. `@app.task` returns a Task INSTANCE and stashes the
    original function on `Task.run` (`run = fun if bind else staticmethod(fun)`),
    so with `bind=False` this reads back the plain function -- measured:
    `type(ping_job.run) is function`. Asserting on `ping_job` itself would be
    vacuous: a Task instance is never a coroutine function, so the assertion would
    hold just as well for an `async def` task.

    Verified to bite: making `ping_job` an `async def` turns this RED
    (`iscoroutinefunction(.run)` -> True). Celery 5.6.3 accepts the decoration
    WITHOUT any complaint and calling the task hands back `<coroutine object>`,
    which is exactly why a guard is needed rather than trust in the framework.
    """
    import inspect

    from fdi.workers.tasks import ping_job

    assert not inspect.iscoroutinefunction(ping_job.run)


def test_ping_job_is_registered_by_worker_boot_discovery(stub_fdi_env: None) -> None:
    """Deferring discovery must not quietly become never DOING it.

    test_celery_app.py::test_task_discovery_is_deferred_not_forced proves the
    `import_modules` signal has a listener, and its docstring claims more than that:
    "Verified that the task genuinely registers that way; deferring must not quietly
    become never, or we trade an ImportError for `Received unregistered task of type
    'fdi.ping_job'`." Nothing checked the registration half -- and until this module
    existed there was no task to check it against. A listener that runs and finds
    nothing is indistinguishable from one that works.

    That failure is total and silent AT BOOT: the worker starts clean, reports
    ready, and then rejects every message the API sends it.

    A SUBPROCESS is what makes this honest, not ceremony. Measured: once anything in
    the process has done `import fdi.workers.tasks` -- as every other test in this
    file does -- `fdi.ping_job` is in `app.tasks` because the DECORATOR put it there,
    and an in-process assertion passes with autodiscovery entirely broken. The probe
    asserts the task is ABSENT before `import_default_modules()` so it cannot
    accidentally credit discovery for an import side-effect.
    """
    result = subprocess.run(
        [sys.executable, "-c", _DISCOVERY_PROBE],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=os.environ.copy(),  # stub_fdi_env put the six FDI_ vars here
        timeout=60,
    )
    detail = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode == 0, f"the discovery probe did not run to completion.\n{detail}"
    assert "REGISTERED" in result.stdout, (
        "fdi.ping_job is NOT registered after the worker's own boot-time discovery "
        f"path ran. Every message the API dispatches would be rejected with "
        f"'Received unregistered task of type fdi.ping_job'.\n{detail}"
    )


def test_correlation_id_does_not_leak_from_one_task_to_the_next(
    stub_fdi_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job dispatched WITHOUT a correlation ID must never log under the previous job's.

    THE test in this file. Prefork runs up to `worker_max_tasks_per_child` (50,
    pinned in test_celery_app.py) tasks in ONE process, and `bind_correlation_id`
    writes a contextvar that outlives the task that set it. Celery resets nothing
    between tasks: `celery/app/trace.py`, `celery/concurrency/prefork.py` and
    `billiard/pool.py` contain no reference to `contextvars` or `copy_context` at
    all -- verified by reading their source, not assumed.

    So a wrapper shaped `if correlation_id: bind_correlation_id(...)` is not a
    cheap guard, it is a leak: job B arrives with no ID, the branch is skipped,
    nothing rebinds, and B's log lines carry A's correlation ID. For a platform
    whose product is provenance, that is a log line pointing at the wrong
    document -- worse than no log line, because it is confidently wrong.

    Measured with that shape in place: A bound 'cid-a', B ran with no ID, and B's
    body read 'cid-a'. This test is RED against it and green against the
    unconditional bind.

    `.apply()` rather than a direct call ON PURPOSE: it runs the task through
    Celery's real `build_tracer`/`trace_task` path, so this proves the framework
    does not save us, not merely that a Python function call does not.
    """
    from fdi.workers import tasks

    seen: list[str] = []

    async def _spy(job_id: str, **kwargs: Any) -> str:
        seen.append(get_correlation_id())
        return f"jobs/{job_id}/ping.json"

    monkeypatch.setattr(tasks, "_ping_job", _spy)

    # Job A: dispatched WITH an ID, as the API does.
    tasks.ping_job.apply(args=["job-a"], kwargs={"correlation_id": "cid-a"}, throw=True)
    assert seen[0] == "cid-a", (
        "the ID never reached the coroutine -- Global Constraint 4: contextvars is "
        "copy-IN across asyncio.run(), so the bind must happen BEFORE the loop"
    )

    # Job B: SAME process, no ID. This is the second of 50 tasks in a prefork child.
    tasks.ping_job.apply(args=["job-b"], throw=True)

    assert seen[1] != "cid-a", (
        "CORRELATION ID LEAK: job B was dispatched with no correlation ID and "
        "inherited job A's. Every log line B emits is attributed to A's document."
    )
    assert seen[1] != "", (
        "job B ran with no correlation ID at all -- its log lines are unattributable. "
        "The wrapper must MINT one when the message carries none."
    )
    uuid.UUID(seen[1])  # raises unless it is a real minted ID


@pytest.mark.parametrize("should_raise", [False, True], ids=["task-succeeds", "task-raises"])
def test_correlation_id_does_not_outlive_the_task(
    stub_fdi_env: None, monkeypatch: pytest.MonkeyPatch, should_raise: bool
) -> None:
    """The clear must be in a `finally`, so the ID dies with the task that owns it.

    The unconditional bind alone stops THIS task from inheriting. Clearing is what
    stops this task's ID becoming the ambient default for whatever runs next in
    the process -- including a future SP-2..SP-18 task whose author forgot to bind,
    and Celery's own loggers between tasks. Defence in depth, in the one module
    every later stage is copied from.

    The `task-raises` param is the half that matters: without `finally` a failing
    task leaves its ID bound forever.
    """
    from fdi.workers import tasks

    async def _impl(job_id: str, **kwargs: Any) -> str:
        if should_raise:
            raise RuntimeError("stage blew up")
        return "jobs/x/ping.json"

    monkeypatch.setattr(tasks, "_ping_job", _impl)

    result = tasks.ping_job.apply(args=["job-1"], kwargs={"correlation_id": "cid-outlives"})
    assert result.state == ("FAILURE" if should_raise else "SUCCESS")

    assert get_correlation_id() == "", (
        "the correlation ID outlived its task. The next task to run in this process "
        "inherits it as a default, and anything logged between tasks is attributed "
        "to a job that has already finished."
    )


# --------------------------------------------------------------------------- #
# The coroutine's contract. Container-backed.
# --------------------------------------------------------------------------- #


@pytest.mark.integration
async def test_ping_job_writes_artifact_and_marks_done(
    stub_fdi_env: None,
    session_factory: async_sessionmaker[AsyncSession],
    minio_client: Minio,
    postgres_url: str,
) -> None:
    from fdi.workers.tasks import _ping_job

    job_id = await _new_job(session_factory, "cid-ping")

    key = await _ping_job(
        job_id, store_bucket="test-artifacts", client=minio_client, db_url=postgres_url
    )

    assert key == f"jobs/{job_id}/ping.json"
    # Read back through a DIFFERENT engine than the task's own: proves the commit
    # actually landed rather than merely being visible to its own session.
    async with session_factory() as s:
        job = await JobRepository(s).get(job_id)
    assert job is not None
    assert job.status == JobStatus.DONE.value
    assert job.artifact_key == key


@pytest.mark.integration
async def test_ping_artifact_body_is_deterministic(
    stub_fdi_env: None,
    session_factory: async_sessionmaker[AsyncSession],
    minio_client: Minio,
    postgres_url: str,
) -> None:
    """Done criterion 10 / Global Constraint 6. The body must not contain the job
    id or a timestamp -- those belong in the Postgres row.

    Three assertions, each catching what the others miss. The pairwise check is
    the invariant itself but it is only a PROBABILISTIC gate against a clock: two
    artifacts written milliseconds apart differ if the body carries a microsecond
    timestamp and are identical if it carries a date. Pinning the exact bytes is
    what closes that -- no timestamp of any granularity, and no id, can hide from
    it. It is deliberately hardcoded rather than derived from `PING_PAYLOAD`,
    which would compare the implementation against itself.
    """
    from fdi.workers.tasks import _ping_job

    ids = [await _new_job(session_factory, "cid") for _ in range(2)]
    for job_id in ids:
        await _ping_job(
            job_id, store_bucket="test-artifacts", client=minio_client, db_url=postgres_url
        )

    store = ArtifactStore(minio_client, bucket="test-artifacts")
    bodies = [await store.get_raw(f"jobs/{i}/ping.json") for i in ids]

    assert bodies[0] == bodies[1], "artifact body must be a pure function of input"
    assert ids[0].encode() not in bodies[0], "the job id leaked into the artifact body"
    assert bodies[0] == b'{"kind":"ping","schema_version":1}', (
        "the ping body is no longer exactly its payload -- something (a clock?) "
        "reached the artifact. IDs and timestamps live in the Postgres row."
    )


@pytest.mark.integration
async def test_ping_job_reads_no_settings_when_every_dependency_is_injected(
    stub_fdi_env: None,
    session_factory: async_sessionmaker[AsyncSession],
    minio_client: Minio,
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An injected caller must not read config it does not use.

    `settings = get_settings()` on its own line, above the `db_url or ...`
    fallbacks, makes every caller pay for the environment even when it supplies
    all three dependencies -- which is why this reads config that is then thrown
    away. Moving the call to the right of each `or` makes the fallback lazy, and
    Python's short-circuit is the whole mechanism.

    Sabotaging the function rather than the environment is deliberate: it proves
    `get_settings` is never CALLED, not merely that the env happens to be readable.
    """
    from fdi.workers import tasks

    def _boom() -> Any:
        raise AssertionError(
            "_ping_job called get_settings() even though db_url, client and "
            "store_bucket were all injected"
        )

    monkeypatch.setattr(tasks, "get_settings", _boom)

    job_id = await _new_job(session_factory, "cid-lazy")
    key = await tasks._ping_job(
        job_id, store_bucket="test-artifacts", client=minio_client, db_url=postgres_url
    )
    assert key == f"jobs/{job_id}/ping.json"


# --------------------------------------------------------------------------- #
# The failure handler must not eat the diagnosis.
# --------------------------------------------------------------------------- #


class _StoreDown(RuntimeError):
    """The ORIGINAL cause: S3 is unreachable."""


class _DatabaseDown(RuntimeError):
    """The BOOKKEEPING failure: the DB broke while recording the original."""


@pytest.mark.integration
async def test_failure_handler_does_not_mask_the_original_cause(
    stub_fdi_env: None,
    session_factory: async_sessionmaker[AsyncSession],
    minio_client: Minio,
    postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `mark_failed` ALSO fails, the real cause must still be what surfaces.

    An exception escaping an `except` block REPLACES the one being handled, so a
    handler that calls `mark_failed` unguarded trades the diagnosis for whatever
    broke while recording it. This is not survivable via `__context__`: measured
    against Celery 5.6.3 with this app's `result_serializer="json"`,
    `prepare_exception` emits only

        {'exc_type': 'KeyError', 'exc_message': (...), 'exc_module': 'builtins'}

    -- no chain -- so a client's `result.get()` re-raises the MASK with
    `__context__` None. The original text survives only in the worker-side
    traceback string. A human grepping logs can recover it; a program cannot.

    Both failures here are RuntimeErrors so the assertion is about WHICH one
    arrived, not about types accidentally differing.
    """
    from fdi.workers.tasks import _ping_job

    job_id = await _new_job(session_factory, "cid-mask")

    def _bucket_exists(*args: Any, **kwargs: Any) -> bool:
        raise _StoreDown("s3 is down")

    async def _mark_failed(self: JobRepository, job_id: str, error: str) -> None:
        raise _DatabaseDown("db is down while recording the failure")

    monkeypatch.setattr(minio_client, "bucket_exists", _bucket_exists)
    monkeypatch.setattr(JobRepository, "mark_failed", _mark_failed)

    with pytest.raises(RuntimeError) as excinfo:
        await _ping_job(
            job_id, store_bucket="test-artifacts", client=minio_client, db_url=postgres_url
        )

    assert isinstance(excinfo.value, _StoreDown), (
        f"the failure handler replaced the real cause with {excinfo.value!r}. Celery "
        "records THIS as the task's error, and json serialisation drops __context__ "
        "-- the actual reason the job died is unrecoverable to any client."
    )


@pytest.mark.integration
async def test_ping_job_on_a_missing_job_surfaces_the_original_keyerror(
    stub_fdi_env: None,
    session_factory: async_sessionmaker[AsyncSession],
    minio_client: Minio,
    postgres_url: str,
) -> None:
    """A job id that does not exist: `mark_running` raises KeyError, then the
    handler's `mark_failed` raises KeyError again on the same missing row.

    The nastiest shape of the masking bug, because both exceptions are `KeyError`
    carrying the SAME message -- so the substitution is invisible to every obvious
    assertion. `__context__` is the only thing that tells them apart: the original
    is raised with no exception being handled, so its context is None, while the
    handler's is chained onto it.
    """
    from fdi.workers.tasks import _ping_job

    missing = str(uuid.uuid4())

    with pytest.raises(KeyError) as excinfo:
        await _ping_job(
            missing, store_bucket="test-artifacts", client=minio_client, db_url=postgres_url
        )

    assert missing in str(excinfo.value)
    assert excinfo.value.__context__ is None, (
        "the KeyError that surfaced is the failure HANDLER's, not the original from "
        f"mark_running -- it is chained onto {excinfo.value.__context__!r}. Same type, "
        "same message, so nothing else here can tell them apart."
    )
