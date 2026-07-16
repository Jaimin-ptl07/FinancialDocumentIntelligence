"""End-to-end eval glue: load dataset -> run candidate -> persist -> aggregate.

The only orchestration layer that touches both the object store (dataset
artifacts) and Postgres (run + case-result persistence) for a single eval
run. Everything it calls is pure or already transaction-scoped; this module
owns the one `commit()`.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdi.core.metrics import ExactMatch, Metric
from fdi.pipeline.dataset_store import get_dataset
from fdi.pipeline.evaluation import CANDIDATES, CandidateRegistry, run_eval
from fdi.platform.db.repository import EvalRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def run_evaluation(
    *,
    run_id: str,
    candidate_name: str,
    dataset_name: str,
    dataset_version: int,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    candidates: CandidateRegistry = CANDIDATES,
    metric: Metric | None = None,
) -> float:
    ds = await get_dataset(store, dataset_name, dataset_version)
    candidate = candidates.get(candidate_name)
    m = metric or ExactMatch()
    outcome = run_eval(candidate, ds, m)
    async with session_factory() as s:
        await EvalRepository(s).record_run(
            run_id,
            candidate.name,
            candidate.version,
            ds.name,
            ds.version,
            m.name,
            outcome,
        )
        await s.commit()
    return outcome.aggregate
