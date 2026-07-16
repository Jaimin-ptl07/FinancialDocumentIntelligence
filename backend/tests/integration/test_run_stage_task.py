"""The generic `run_stage` Celery task -- mirrors test_ping_task.py's shape.

Split the same way as test_ping_task.py: the wrapper's contract (no container,
just `stub_fdi_env`) and the coroutine's contract (container-backed).
"""

import inspect
import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.keys import artifact_key, root_input_hash
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import RegistryRepository


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


def test_run_stage_task_is_not_a_coroutine_function(stub_fdi_env: None) -> None:
    """Same idiom as test_ping_job_task_is_not_a_coroutine_function: `@app.task`
    with `bind=False` stashes the plain function on `Task.run`, so assert on
    THAT, not on the Task instance itself (which is never a coroutine function
    regardless of how the underlying task was defined)."""
    from fdi.workers.tasks import run_stage_task

    assert not inspect.iscoroutinefunction(run_stage_task.run)


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
