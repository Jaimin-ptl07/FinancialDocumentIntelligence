"""`fdi-mirror` -- the deploy-day data-mirror tool (productionization T10).

Driven through its CLI entry point against a real Postgres (a database of its
OWN, so the corpus-wide check sees only what these tests seed) and a real MinIO
(a bucket of its own, for the same reason). Assertions are on what an operator
sees: exit status, printed lines, and what is left in the object store.
"""

import asyncio
import io
import uuid
from collections.abc import Iterator

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from minio import Minio
from sqlalchemy.ext.asyncio import create_async_engine

from fdi.pipeline.intake import document_key
from fdi.pipeline.keys import artifact_key
from fdi.platform.config import get_settings
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import RegistryRepository
from tests.conftest import TEST_SIGNING_SECRET
from tests.integration.conftest import _SETTINGS_STUBS, BACKEND_DIR

pytestmark = pytest.mark.integration


@pytest.fixture
def mirror_db_url(postgres_url: str) -> Iterator[str]:
    """A freshly migrated database in the session's Postgres container.

    The check is corpus-wide by design, and the shared test database holds
    every other test's stage runs -- whose artifacts live in buckets this
    module never sees. A database per TEST (not per module: these tests seed
    into it, and the check would see a neighbour's deliberate gap) is the only
    way an "everything is consistent" assertion can mean anything.
    """
    name = f"mirror_{uuid.uuid4().hex[:12]}"
    admin_dsn = postgres_url.replace("postgresql+asyncpg://", "postgresql://")

    async def _admin(sql: str) -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    url = postgres_url.rsplit("/", 1)[0] + f"/{name}"
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("FDI_DATABASE_URL", url)
        for key, value in _SETTINGS_STUBS.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    yield url
    # FORCE: a test's engine may still hold a pooled connection at teardown.
    asyncio.run(_admin(f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture
def bucket(minio_client: Minio) -> str:
    name = f"mirror-{uuid.uuid4().hex[:12]}"
    minio_client.make_bucket(name)
    return name


@pytest.fixture
def mirror_env(mirror_db_url, minio_endpoint, bucket, monkeypatch):
    """The env `fdi-mirror`'s own Settings build reads in the container."""
    for key, value in {
        "FDI_DATABASE_URL": mirror_db_url,
        "FDI_BROKER_URL": "amqp://guest:guest@localhost:5672/%2F",
        "FDI_RESULT_BACKEND": "redis://localhost:6379/0",
        "FDI_S3_ENDPOINT": minio_endpoint,
        "FDI_S3_ACCESS_KEY": "minioadmin",
        "FDI_S3_SECRET_KEY": "minioadmin",
        "FDI_S3_BUCKET": bucket,
        "FDI_AUTH_SECRET": TEST_SIGNING_SECRET,
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def seed(mirror_db_url, minio_client, bucket):
    """Seed one document: its source object plus one `done` run per stage.

    `present=False` records the row but leaves its object out of the bucket --
    exactly the state a half-finished object-store sync leaves behind.
    """
    engine = create_async_engine(mirror_db_url)
    factory = make_session_factory(engine)

    async def _put(key: str) -> None:
        minio_client.put_object(bucket, key, io.BytesIO(b"{}"), 2)

    async def _seed(
        stages: dict[str, bool], *, source_present: bool = True
    ) -> str:
        doc_id = str(uuid.uuid4())
        checksum = uuid.uuid4().hex * 2
        async with factory() as s:
            repo = RegistryRepository(s)
            await repo.create_document(doc_id, checksum=checksum)
            for stage, present in stages.items():
                run_id = str(uuid.uuid4())
                input_hash = uuid.uuid4().hex * 2
                key = artifact_key(doc_id, stage, 1, input_hash)
                await repo.start_run(run_id, doc_id, stage, 1, input_hash, None)
                await repo.finish_run(run_id, "0" * 64, key)
                if present:
                    await _put(key)
            await s.commit()
        if source_present:
            await _put(document_key(checksum))
        return doc_id

    yield _seed
    await engine.dispose()


def _objects(minio_client: Minio, bucket: str) -> set[str]:
    return {
        o.object_name
        for o in minio_client.list_objects(bucket, recursive=True)
        if o.object_name is not None
    }


async def test_check_passes_when_every_recorded_artifact_is_in_the_store(
    mirror_env, seed, capsys
) -> None:
    from fdi.workers.mirror_cli import amain

    await seed({"ocr": True, "semantic": True})

    await amain(["check"])

    out = capsys.readouterr().out
    assert "0 missing" in out


async def test_check_names_each_missing_artifact_and_fails(
    mirror_env, seed, capsys
) -> None:
    from fdi.workers.mirror_cli import amain

    doc_id = await seed({"ocr": True, "semantic": False})

    with pytest.raises(SystemExit) as exc:
        await amain(["check"])

    assert exc.value.code == 1
    out = capsys.readouterr().out
    missing = [line for line in out.splitlines() if line.startswith("MISSING")]
    assert len(missing) == 1
    assert doc_id in missing[0]
    assert "semantic" in missing[0]


async def test_check_reports_a_missing_source_document(
    mirror_env, seed, capsys
) -> None:
    from fdi.workers.mirror_cli import amain

    doc_id = await seed({"ocr": True}, source_present=False)

    with pytest.raises(SystemExit) as exc:
        await amain(["check"])

    assert exc.value.code == 1
    missing = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("MISSING")
    ]
    assert len(missing) == 1
    assert doc_id in missing[0]
    assert "source" in missing[0]


@pytest.fixture
def dispatched(monkeypatch) -> list[tuple[str, str]]:
    """Record what `replay` publishes instead of reaching a broker. The task's
    own execution is covered by the stage tests; this asserts the hand-off."""
    from fdi.workers import tasks

    calls: list[tuple[str, str]] = []

    def _apply_async(args, kwargs):
        calls.append((args[0], args[1]))

    monkeypatch.setattr(tasks.run_stage_task, "apply_async", _apply_async)
    return calls


async def test_replay_clears_side_effect_receipts_and_redispatches_them(
    mirror_env, seed, minio_client, bucket, dispatched
) -> None:
    from fdi.workers.mirror_cli import amain

    doc_id = await seed(
        {"embedding": True, "chunks": True, "index": True, "graph": True, "graph_load": True}
    )

    await amain(["replay"])

    left = _objects(minio_client, bucket)
    # A cache hit on these receipts is what would skip the Milvus/Neo4j write.
    assert not any(f"/{doc_id}/index/" in k for k in left)
    assert not any(f"/{doc_id}/graph_load/" in k for k in left)
    # Everything else is the mirrored corpus and must be untouched.
    for stage in ("embedding", "chunks", "graph"):
        assert any(f"/{doc_id}/{stage}/" in k for k in left)
    assert sorted(dispatched) == [(doc_id, "graph_load"), (doc_id, "index")]


async def test_replay_only_dispatches_stages_the_document_had_reached(
    mirror_env, seed, dispatched
) -> None:
    from fdi.workers.mirror_cli import amain

    indexed_only = await seed({"embedding": True, "chunks": True, "index": True})
    untouched = await seed({"ocr": True})

    await amain(["replay"])

    assert dispatched == [(indexed_only, "index")]
    assert all(doc != untouched for doc, _ in dispatched)


async def test_replay_dry_run_changes_nothing(
    mirror_env, seed, minio_client, bucket, dispatched, capsys
) -> None:
    from fdi.workers.mirror_cli import amain

    doc_id = await seed({"embedding": True, "chunks": True, "index": True})
    before = _objects(minio_client, bucket)

    await amain(["replay", "--dry-run"])

    assert _objects(minio_client, bucket) == before
    assert dispatched == []
    assert f"{doc_id}\tindex" in capsys.readouterr().out
