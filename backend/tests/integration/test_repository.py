"""RegistryRepository against a real PostgreSQL 18.4.

These run against the live CHECK constraint from migration 0002, so any status
this class writes that is outside ('queued','running','done','failed') fails
here as an IntegrityError rather than in a later stage.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.facts import RunFacts
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import RegistryRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


async def test_run_lifecycle_and_facts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Unique doc + run ids: the migrated DB is session-scoped, so hardcoded literals
    # collide with the same ids in sibling tests on the PKs.
    async with session_factory() as s:
        repo = RegistryRepository(s)
        doc, r1 = str(uuid.uuid4()), str(uuid.uuid4())
        await repo.create_document(doc)
        await repo.start_run(r1, doc, "ping", 1, "ih", "corr-1")
        await repo.finish_run(r1, output_hash="oh", artifact_key=f"artifacts/{doc}/ping/v1/ih.json")
        await s.commit()

        facts = await repo.runs_for_document(doc)
        assert facts == {"ping": RunFacts(version=1, input_hash="ih", output_hash="oh")}

        run = await repo.latest_run(doc, "ping")
        assert run is not None and run.status == "done" and run.artifact_key.endswith("ih.json")


async def test_failed_run_is_recorded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as s:
        repo = RegistryRepository(s)
        doc, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        await repo.create_document(doc)
        await repo.start_run(r2, doc, "ping", 1, "ih", None)
        await repo.fail_run(r2, "boom")
        await s.commit()

        run = await repo.latest_run(doc, "ping")
        assert run is not None
        assert run.status == "failed" and run.error == "boom"
        assert await repo.runs_for_document(doc) == {}  # only DONE runs are facts
