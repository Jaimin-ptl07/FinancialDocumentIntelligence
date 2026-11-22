"""The generic `run_stage` Celery task -- mirrors SP-1's `ping_job` shape.

Split the same way SP-1's task tests were: the wrapper's contract (no
container, just `stub_fdi_env`) and the coroutine's contract (container-backed).
"""

import inspect
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.keys import artifact_key, root_input_hash
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import RegistryRepository

BACKEND = Path(__file__).resolve().parents[2]

# Runs in a FRESH interpreter -- see test_run_stage_task_is_registered_by_worker_boot_discovery.
_DISCOVERY_PROBE = """
from fdi.workers.app import app

assert "fdi.run_stage" not in app.tasks, (
    "importing fdi.workers.app alone registered the task, so this probe cannot "
    "attribute registration to autodiscovery"
)
app.loader.import_default_modules()
print("REGISTERED" if "fdi.run_stage" in app.tasks else "MISSING")
# T5: the API-job task rides the same module, so the same discovery must find it.
print("JOB-REGISTERED" if "fdi.run_job" in app.tasks else "JOB-MISSING")
"""


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


def test_run_stage_task_is_not_a_coroutine_function(stub_fdi_env: None) -> None:
    """`@app.task` with `bind=False` stashes the plain function on `Task.run`, so
    assert on THAT, not on the Task instance itself (which is never a coroutine
    function regardless of how the underlying task was defined)."""
    from fdi.workers.tasks import run_stage_task

    assert not inspect.iscoroutinefunction(run_stage_task.run)


def test_run_stage_task_is_registered_by_worker_boot_discovery(stub_fdi_env: None) -> None:
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
    file does -- `fdi.run_stage` is in `app.tasks` because the DECORATOR put it there,
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
        "fdi.run_stage is NOT registered after the worker's own boot-time discovery "
        f"path ran. Every message the API dispatches would be rejected with "
        f"'Received unregistered task of type fdi.run_stage'.\n{detail}"
    )
    assert "JOB-REGISTERED" in result.stdout, (
        "fdi.run_job is NOT registered by worker boot discovery -- every job "
        f"the expensive routes queue would be rejected.\n{detail}"
    )


@pytest.mark.integration
async def test_run_stage_async_round_trip(
    stub_fdi_env: None,
    session_factory: async_sessionmaker[AsyncSession],
    minio_client: Minio,
    postgres_url: str,
) -> None:
    from fdi.workers.tasks import _run_stage_async

    # Document.id is String(36) -- a bare uuid4() string is exactly 36 chars;
    # a prefixed one (e.g. f"doc-{uuid.uuid4()}") overflows the column.
    doc_id = str(uuid.uuid4())
    async with session_factory() as s:
        await RegistryRepository(s).create_document(doc_id)
        await s.commit()

    successors = await _run_stage_async(
        doc_id,
        "ping",
        str(uuid.uuid4()),
        "c1",
        db_url=postgres_url,
        client=minio_client,
        store_bucket="test-artifacts",
    )

    assert successors == [], "ping has no successors registered"

    async with session_factory() as s:
        run = await RegistryRepository(s).latest_run(doc_id, "ping")
    assert run is not None and run.status == "done"

    from fdi.platform.storage.artifact_store import ArtifactStore

    store = ArtifactStore(minio_client, bucket="test-artifacts")
    key = artifact_key(doc_id, "ping", 1, root_input_hash(None))
    assert await store.exists(key)


@pytest.mark.integration
async def test_completing_the_issuer_stage_files_the_report_under_its_company(
    stub_fdi_env: None,
    session_factory: async_sessionmaker[AsyncSession],
    minio_client: Minio,
    postgres_url: str,
) -> None:
    """The worker resolves the Report against the company registry as soon as
    the issuer stage completes -- stages have no Postgres access, so the
    linking is the task's job, not the stage's."""
    from fdi.pipeline.companies import CompanyRegistry
    from fdi.platform.storage.artifact_store import ArtifactStore
    from fdi.workers.tasks import _run_stage_async

    store = ArtifactStore(minio_client, bucket="test-artifacts")
    await store.ensure_bucket()
    n = uuid.uuid4().int
    cin = f"L{n % 10**5:05d}KA2001PLC{(n // 10**5) % 10**6:06d}"
    doc_id = str(uuid.uuid4())
    upstream = {
        "ast": {"sections": [{"heading": "Notice", "paragraphs": [
            f"Gamma Mills Limited CIN: {cin} Registered Office: Bengaluru"
        ]}]},
        "provenance": {"node_provenance": [
            {"node_id": "s0", "page": 2}, {"node_id": "s0p0", "page": 2}
        ]},
    }
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.create_document(doc_id)
        for stage, body in upstream.items():
            run_id = str(uuid.uuid4())
            key = f"test/{doc_id}/{stage}.json"
            await store.put_json(key, body)
            await repo.start_run(run_id, doc_id, stage, 1, stage, "c1")
            await repo.finish_run(run_id, stage, key)
        await s.commit()

    await _run_stage_async(
        doc_id, "issuer", str(uuid.uuid4()), "c1",
        db_url=postgres_url, client=minio_client, store_bucket="test-artifacts",
    )

    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        (company,) = [c for c in await registry.list_companies() if c.cin == cin]
        reports = await registry.company_reports(company.id)
    assert company.name == "Gamma Mills Limited"
    assert [r.doc_id for r in reports] == [doc_id]
