"""DocumentRepository against a real PostgreSQL 18.4.

Mirrors test_repository.py's session_factory fixture -- the migrated DB is
session-scoped, so every id/checksum here is a fresh uuid to avoid PK/unique
collisions with sibling tests.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import DocumentRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


async def test_create_get_and_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = f"key-{uuid.uuid4()}"
    async with session_factory() as s:
        repo = DocumentRepository(s)
        assert await repo.next_version(key) == 1
        d1 = await repo.create(
            id=str(uuid.uuid4()),
            checksum=str(uuid.uuid4()),
            filename="a.pdf",
            byte_size=10,
            content_type="application/pdf",
            doc_key=key,
            version=1,
        )
        await s.commit()
    async with session_factory() as s:
        repo = DocumentRepository(s)
        got = await repo.get(d1.id)
        assert got is not None and got.filename == "a.pdf"
        assert await repo.next_version(key) == 2
        assert await repo.get_by_checksum(d1.checksum) is not None
        d2 = await repo.create(
            id=str(uuid.uuid4()),
            checksum=str(uuid.uuid4()),
            filename="a2.pdf",
            byte_size=11,
            content_type="application/pdf",
            doc_key=key,
            version=2,
        )
        await s.commit()
        versions = await repo.list_by_doc_key(key)
    assert [d.version for d in versions] == [1, 2]
    assert d2.filename == "a2.pdf"
