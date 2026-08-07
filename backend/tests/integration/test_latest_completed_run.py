"""Reading a stage's OUTPUT must find the run that produced one, not merely
the newest row.

A stage holds several runs at once. Reprocessing RIL from `basis` left
`fog_mapping` with a `done` run and a NEWER `running` one; `run_stage`'s
dependency read took the newer row, found a NULL artifact_key, and `fog_reason`
died with a bare `AssertionError()`. The same lookup backs
`GET /documents/{id}/artifacts/{stage}`, so the UI reported "not built yet" for
artifacts that existed.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import RegistryRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


async def _doc(session_factory) -> str:
    doc_id = str(uuid.uuid4())
    async with session_factory() as s:
        await RegistryRepository(s).create_document(doc_id)
        await s.commit()
    return doc_id


async def test_a_running_rerun_does_not_hide_the_completed_artifact(
    session_factory,
) -> None:
    """The RIL failure, reproduced: done first, running second."""
    doc_id = await _doc(session_factory)
    done_id, running_id = str(uuid.uuid4()), str(uuid.uuid4())

    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.start_run(done_id, doc_id, "fog_mapping", 9, "h1", "c1")
        await repo.finish_run(done_id, "out1", "artifacts/doc/fog_mapping/v9/h1.json")
        await s.commit()

    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.start_run(running_id, doc_id, "fog_mapping", 9, "h2", "c2")
        await s.commit()

    async with session_factory() as s:
        repo = RegistryRepository(s)
        newest = await repo.latest_run(doc_id, "fog_mapping")
        completed = await repo.latest_completed_run(doc_id, "fog_mapping")

    # `latest_run` still answers "what is this stage doing?" -- unchanged.
    assert newest is not None and newest.id == running_id
    assert newest.artifact_key is None
    # `latest_completed_run` answers "give me its output".
    assert completed is not None and completed.id == done_id
    assert completed.artifact_key == "artifacts/doc/fog_mapping/v9/h1.json"


async def test_a_failed_rerun_does_not_hide_the_completed_artifact(
    session_factory,
) -> None:
    """A re-run that FAILED leaves the previous artifact the only answer there
    is -- discarding it would turn one failed run into a lost document."""
    doc_id = await _doc(session_factory)
    done_id, failed_id = str(uuid.uuid4()), str(uuid.uuid4())

    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.start_run(done_id, doc_id, "fog_reason", 4, "h1", "c1")
        await repo.finish_run(done_id, "out1", "artifacts/doc/fog_reason/v4/h1.json")
        await s.commit()

    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.start_run(failed_id, doc_id, "fog_reason", 4, "h2", "c2")
        await repo.fail_run(failed_id, "AssertionError()")
        await s.commit()

    async with session_factory() as s:
        completed = await RegistryRepository(s).latest_completed_run(doc_id, "fog_reason")
    assert completed is not None and completed.id == done_id


async def test_the_newest_completed_run_wins_when_several_completed(
    session_factory,
) -> None:
    """Not "the first that ever succeeded" -- the LATEST, so a genuine re-run
    supersedes its predecessor."""
    doc_id = await _doc(session_factory)
    first, second = str(uuid.uuid4()), str(uuid.uuid4())

    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.start_run(first, doc_id, "fog_objects", 6, "h1", "c1")
        await repo.finish_run(first, "out1", "artifacts/doc/fog_objects/v6/h1.json")
        await s.commit()

    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.start_run(second, doc_id, "fog_objects", 6, "h2", "c2")
        await repo.finish_run(second, "out2", "artifacts/doc/fog_objects/v6/h2.json")
        await s.commit()

    async with session_factory() as s:
        completed = await RegistryRepository(s).latest_completed_run(doc_id, "fog_objects")
    assert completed is not None and completed.id == second


async def test_no_completed_run_returns_none_rather_than_a_running_one(
    session_factory,
) -> None:
    doc_id = await _doc(session_factory)
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.start_run(str(uuid.uuid4()), doc_id, "fog_mapping", 9, "h1", "c1")
        await s.commit()

    async with session_factory() as s:
        assert await RegistryRepository(s).latest_completed_run(doc_id, "fog_mapping") is None
