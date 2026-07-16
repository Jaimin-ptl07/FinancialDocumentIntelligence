"""eval_service end-to-end glue against real Postgres + MinIO.

Seeds the toy dataset into a real object store, runs the uppercase candidate
through `run_evaluation`, and asserts both the returned aggregate and the
persisted leaderboard row it produces.
"""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.candidates.uppercase import UppercaseCandidate, toy_dataset
from fdi.pipeline.dataset_store import put_dataset
from fdi.pipeline.eval_service import run_evaluation
from fdi.pipeline.evaluation import CandidateRegistry
from fdi.platform.db.engine import make_session_factory
from fdi.platform.db.repository import EvalRepository
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


async def test_eval_service_persists_and_ranks(
    session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore
) -> None:
    await put_dataset(store, toy_dataset())
    reg = CandidateRegistry()
    reg.register(UppercaseCandidate())
    agg = await run_evaluation(
        run_id=str(uuid.uuid4()),
        candidate_name="uppercase",
        dataset_name="toy",
        dataset_version=1,
        session_factory=session_factory,
        store=store,
        candidates=reg,
    )
    assert agg == 1.0  # uppercase matches the toy dataset exactly
    async with session_factory() as s:
        board = await EvalRepository(s).leaderboard("toy", "exact_match")
    assert board[0] == ("uppercase", 1, 1.0)
