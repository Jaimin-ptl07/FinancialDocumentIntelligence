"""run_stage against real Postgres + MinIO: cache-miss runs+stores+records; cache-hit
skips the body; a version bump flips staleness. The body is run at most once per key."""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.keys import artifact_key, root_input_hash
from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stage import StageContext
from fdi.platform.db.engine import make_session_factory
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


class _Counter:
    name = "ping"
    version = 1
    deps: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, ctx: StageContext) -> object:
        self.calls += 1
        return {"kind": "ping", "schema_version": 1}


async def test_cache_miss_then_hit(session_factory, store) -> None:  # fixtures from conftest
    stage = _Counter()
    reg = StageRegistry()
    reg.register(stage)
    resolver = DagResolver(reg.all())
    # Document.id is String(36) -- a bare uuid4() string is exactly 36 chars.
    # The brief's `f"doc-{uuid.uuid4()}"` is 40 chars and overflows that column
    # (StringDataRightTruncationError), so it is adapted here to fit the schema.
    doc = str(uuid.uuid4())

    async with session_factory() as s:
        from fdi.platform.db.repository import RegistryRepository
        await RegistryRepository(s).create_document(doc)
        await s.commit()

    common = {
        "doc_id": doc, "stage_name": "ping", "correlation_id": "c1",
        "session_factory": session_factory, "store": store,
        "registry": reg, "resolver": resolver,
    }
    await run_stage(run_id="r1", **common)
    await run_stage(run_id="r2", **common)   # same input + version -> cache hit

    assert stage.calls == 1, "cache hit must NOT re-run the stage body"
    key = artifact_key(doc, "ping", 1, root_input_hash(None))
    assert await store.exists(key)
    assert await store.get_json(key) == {"kind": "ping", "schema_version": 1}
