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


async def test_purge_expired_refresh_tokens_removes_only_the_dead(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """UserRepository.purge_expired_refresh_tokens: expired rows go, live and
    revoked-but-unexpired rows stay. Repository seam (like everything else in
    this file) because the purge is invisible at the HTTP one by design --
    it is housekeeping, not behavior."""
    from datetime import UTC, datetime, timedelta

    import sqlalchemy

    from fdi.platform.db.models import AppUser, RefreshToken
    from fdi.platform.db.repository import UserRepository
    from fdi.platform.security import hash_password

    now = datetime.now(UTC)
    async with session_factory() as s:
        user = AppUser(
            id=str(uuid.uuid4()),
            email=f"purge-{uuid.uuid4().hex}@example.test",
            password_hash=hash_password("x"),
        )
        s.add(user)
        rows = {
            "expired": RefreshToken(
                id=str(uuid.uuid4()), user_id=user.id, token_hash=uuid.uuid4().hex,
                expires_at=now - timedelta(days=1),
            ),
            "revoked-not-yet-expired": RefreshToken(
                id=str(uuid.uuid4()), user_id=user.id, token_hash=uuid.uuid4().hex,
                expires_at=now + timedelta(days=1), revoked_at=now - timedelta(hours=1),
            ),
            "live": RefreshToken(
                id=str(uuid.uuid4()), user_id=user.id, token_hash=uuid.uuid4().hex,
                expires_at=now + timedelta(days=1),
            ),
        }
        s.add_all(rows.values())
        await s.commit()

        # >= 1, not == 1: the Postgres container is session-scoped across the
        # whole suite, so another file's born-expired rows (test_auth's
        # negative-TTL test) may also be swept by this call. The precise
        # invariant is per-user, asserted below.
        purged = await UserRepository(s).purge_expired_refresh_tokens(now=now)
        await s.commit()
        assert purged >= 1

        remaining_for_user = set(
            (
                await s.execute(
                    sqlalchemy.select(RefreshToken.token_hash).where(
                        RefreshToken.user_id == user.id
                    )
                )
            ).scalars()
        )
        assert remaining_for_user == {
            rows["live"].token_hash,
            rows["revoked-not-yet-expired"].token_hash,
        }
