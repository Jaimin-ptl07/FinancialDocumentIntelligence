"""EvalRepository against a real PostgreSQL 18.4.

Mirrors test_repository.py's session_factory fixture — the migrated DB is
session-scoped, so every id here is a fresh uuid to avoid PK collisions with
sibling tests.
"""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.core.eval_types import CaseResult, EvalOutcome
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import EvalRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


async def test_record_and_leaderboard(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    dataset = f"toy-{uuid.uuid4()}"
    async with session_factory() as s:
        repo = EvalRepository(s)
        out_lo = EvalOutcome(0.4, (CaseResult("c1", 0.4),))
        out_hi = EvalOutcome(0.9, (CaseResult("c1", 0.9),))
        await repo.record_run(str(uuid.uuid4()), "lo", 1, dataset, 1, "exact_match", out_lo)
        await repo.record_run(str(uuid.uuid4()), "hi", 1, dataset, 1, "exact_match", out_hi)
        await s.commit()
        board = await repo.leaderboard(dataset, "exact_match")
    assert board[0][0] == "hi" and board[0][2] == 0.9
    assert board[1][0] == "lo"


async def test_leaderboard_uses_latest_run_per_candidate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A candidate's older, worse run must not shadow their newer, better one.

    Each run is recorded in its own transaction (separate `async with` block,
    each committed before the next starts) rather than batched into one — the
    two `record_run` calls must land in distinct transactions so `created_at`
    (server-side `now()`, which is the *transaction* start time in Postgres)
    actually differs between them.
    """
    dataset = f"toy-{uuid.uuid4()}"
    old = EvalOutcome(0.1, (CaseResult("c1", 0.1),))
    new = EvalOutcome(0.95, (CaseResult("c1", 0.95),))

    async with session_factory() as s:
        await EvalRepository(s).record_run(
            str(uuid.uuid4()), "solo", 1, dataset, 1, "exact_match", old
        )
        await s.commit()

    async with session_factory() as s:
        await EvalRepository(s).record_run(
            str(uuid.uuid4()), "solo", 2, dataset, 1, "exact_match", new
        )
        await s.commit()

    async with session_factory() as s:
        board = await EvalRepository(s).leaderboard(dataset, "exact_match")
    assert len(board) == 1
    assert board[0] == ("solo", 2, 0.95)
