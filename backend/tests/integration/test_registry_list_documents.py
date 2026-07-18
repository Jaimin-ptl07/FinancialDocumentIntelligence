"""RegistryRepository.list_documents() against a real PostgreSQL 18.4.

The migrated DB is session-scoped and nothing truncates it between tests (the
established pattern here -- see test_repository.py / test_document_repository.py
-- is fresh uuids per test, never literal ids or an assumed-empty table,
precisely because sibling tests may already have inserted rows). So this
asserts list_documents()'s CONTRACT -- the whole corpus comes back sorted by
id, and three freshly inserted (out-of-order) rows round-trip in id order --
rather than an exact row count or literal global emptiness, neither of which a
shared, uncleared database across the whole integration suite can guarantee.
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


async def test_list_documents_returns_all_ordered_by_id(session_factory) -> None:
    # `document.id` is String(36) -- a bare uuid4, not a prefixed one, to stay
    # inside the column width (matches the sibling repository tests' ids).
    ids = sorted(str(uuid.uuid4()) for _ in range(3))
    async with session_factory() as s:
        repo = RegistryRepository(s)
        for doc_id in (ids[1], ids[2], ids[0]):  # insert deliberately out of order
            await repo.create_document(doc_id)
        await s.commit()

    async with session_factory() as s:
        docs = await RegistryRepository(s).list_documents()

    all_ids = [d.id for d in docs]
    assert all_ids == sorted(all_ids)  # the whole corpus is id-ordered
    assert [i for i in all_ids if i in ids] == ids  # our three, in id order


async def test_list_documents_never_raises_on_the_current_corpus(session_factory) -> None:
    async with session_factory() as s:
        docs = await RegistryRepository(s).list_documents()
    assert isinstance(docs, list)
