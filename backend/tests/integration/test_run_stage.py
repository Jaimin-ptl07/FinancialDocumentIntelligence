"""run_stage against real Postgres + MinIO: cache-miss runs+stores+records; cache-hit
skips the body; a version bump flips staleness. The body is run at most once per key."""

import uuid
from collections.abc import Mapping

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.hashing import sha256_hex
from fdi.core.serialization import canonical_json_bytes
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
    # Unique run ids: the migrated DB is session-scoped, so a hardcoded "r1"/"r2"
    # collides with the same literals in test_repository.py on stage_run's PK.
    await run_stage(run_id=str(uuid.uuid4()), **common)
    await run_stage(run_id=str(uuid.uuid4()), **common)   # same input + version -> cache hit

    assert stage.calls == 1, "cache hit must NOT re-run the stage body"
    key = artifact_key(doc, "ping", 1, root_input_hash(None))
    assert await store.exists(key)
    assert await store.get_json(key) == {"kind": "ping", "schema_version": 1}


async def test_read_source_and_services_reach_the_stage(session_factory, store) -> None:
    """StageContext.read_source loads the doc's source PDF via the store, and
    ctx.services carries the caller-supplied services bag through to the stage."""
    from fdi.pipeline.intake import document_key
    from fdi.platform.db.repository import DocumentRepository

    doc = str(uuid.uuid4())
    checksum = str(uuid.uuid4())
    await store.put_bytes(document_key(checksum), b"PDFBYTES", "application/pdf")
    async with session_factory() as s:
        await DocumentRepository(s).create(
            id=doc, checksum=checksum, filename="a.pdf",
            byte_size=8, content_type="application/pdf", doc_key=doc, version=1,
        )
        await s.commit()

    class _Probe:
        name = "probe"
        version = 1
        deps: tuple[str, ...] = ()

        async def run(self, ctx: StageContext) -> object:
            return {"src": (await ctx.read_source()).decode(), "svc": ctx.services["greet"]}

    reg = StageRegistry()
    reg.register(_Probe())
    resolver = DagResolver(reg.all())
    await run_stage(
        doc_id=doc, stage_name="probe", run_id=str(uuid.uuid4()), correlation_id="c1",
        session_factory=session_factory, store=store, registry=reg, resolver=resolver,
        services={"greet": "hi"},
    )

    body = await store.get_json(artifact_key(doc, "probe", 1, root_input_hash(checksum.encode())))
    assert body == {"src": "PDFBYTES", "svc": "hi"}


class _SchemaAware:
    """A root stage whose output depends on an INJECTED SERVICE, not a declared
    dep -- the shape review finding I4 describes. `key_material` is what closes
    the hole (fdi.pipeline.stage.Stage.key_material's docstring)."""

    name = "schema_aware"
    version = 1
    deps: tuple[str, ...] = ()

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, ctx: StageContext) -> object:
        self.calls += 1
        return {"seen_schema": ctx.services.get("schema")}

    def key_material(self, services: Mapping[str, object]) -> str:
        schema = services.get("schema")
        if not isinstance(schema, Mapping):
            return ""
        return sha256_hex(canonical_json_bytes(schema))


async def test_a_service_derived_schema_change_busts_the_cache(session_factory, store) -> None:
    """Closes I4 end-to-end through `run_stage`: the taxonomy schema arrives via
    `ctx.services`, outside `stage.deps`, so without folding `key_material` into
    `input_hash` a schema edit would change what the stage WOULD produce while
    the old artifact keeps being served. Same schema -> same key -> cache hit
    (no re-run); different schema -> different key -> cache miss (re-run)."""
    stage = _SchemaAware()
    reg = StageRegistry()
    reg.register(stage)
    resolver = DagResolver(reg.all())
    doc = str(uuid.uuid4())

    async with session_factory() as s:
        from fdi.platform.db.repository import RegistryRepository
        await RegistryRepository(s).create_document(doc)
        await s.commit()

    common = {
        "doc_id": doc, "stage_name": "schema_aware", "correlation_id": "c1",
        "session_factory": session_factory, "store": store,
        "registry": reg, "resolver": resolver,
    }
    schema_a = {"modules": [{"module_id": "pl"}]}
    schema_b = {"modules": [{"module_id": "bs"}]}

    await run_stage(run_id=str(uuid.uuid4()), services={"schema": schema_a}, **common)
    await run_stage(run_id=str(uuid.uuid4()), services={"schema": schema_a}, **common)
    assert stage.calls == 1, "an unchanged schema must be a cache HIT, not a re-run"

    await run_stage(run_id=str(uuid.uuid4()), services={"schema": schema_b}, **common)
    assert stage.calls == 2, "a changed schema must be a cache MISS, or I4 is still open"
