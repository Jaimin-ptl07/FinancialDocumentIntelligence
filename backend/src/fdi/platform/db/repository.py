"""Job persistence. The caller owns the transaction — commit is theirs, not ours.

This class is the sole writer of `jobs`, and `Job.updated_at` depends on that
staying true: `onupdate=func.now()` is a SQLAlchemy-level default, so it only
fires for writes this ORM builds. The mutators below deliberately mutate loaded
ORM attributes — that path emits `SET ..., updated_at=now()`. Reaching for
`ON CONFLICT DO UPDATE` here would silently stop `updated_at` advancing (the
upsert emits no `updated_at` in its SET clause) and every staleness check built
on the column would quietly start lying. See the note on `models.Job.updated_at`.
"""

import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.facts import RunFacts
from fdi.platform.db.models import Document, Job, JobStatus, StageRun


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, job_id: str, correlation_id: str) -> Job:
        job = Job(id=job_id, status=JobStatus.QUEUED.value, correlation_id=correlation_id)
        self._session.add(job)
        await self._session.flush()
        return job

    async def get(self, job_id: str) -> Job | None:
        return await self._session.get(Job, job_id)

    async def _require(self, job_id: str) -> Job:
        job = await self._session.get(Job, job_id)
        if job is None:
            raise KeyError(f"job {job_id} not found")
        return job

    async def mark_running(self, job_id: str) -> None:
        (await self._require(job_id)).status = JobStatus.RUNNING.value

    async def mark_done(self, job_id: str, artifact_key: str) -> None:
        job = await self._require(job_id)
        job.status = JobStatus.DONE.value
        job.artifact_key = artifact_key

    async def mark_failed(self, job_id: str, error: str) -> None:
        job = await self._require(job_id)
        job.status = JobStatus.FAILED.value
        # Truncate rather than let PostgreSQL reject the write: varchar(2048)
        # RAISES StringDataRightTruncationError on overflow, which would abort the
        # transaction and leave the job un-marked -- losing the whole failure, not
        # just the tail of its message. Slicing is codepoints and varchar(n) counts
        # characters (measured, incl. 4-byte chars), so the two agree exactly.
        job.error = error[:2048]


class RegistryRepository:
    """Document + stage_run persistence. The caller owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create_document(self, doc_id: str, checksum: str | None = None) -> None:
        self._s.add(Document(id=doc_id, checksum=checksum))
        await self._s.flush()

    async def start_run(
        self,
        run_id: str,
        doc_id: str,
        stage_name: str,
        stage_version: int,
        input_hash: str,
        correlation_id: str | None,
    ) -> None:
        self._s.add(
            StageRun(
                id=run_id,
                doc_id=doc_id,
                stage_name=stage_name,
                stage_version=stage_version,
                input_hash=input_hash,
                status="running",
                correlation_id=correlation_id,
            )
        )
        await self._s.flush()

    async def finish_run(self, run_id: str, output_hash: str, artifact_key: str) -> None:
        run = await self._s.get(StageRun, run_id)
        assert run is not None
        run.status, run.output_hash, run.artifact_key = "done", output_hash, artifact_key
        await self._s.flush()

    async def fail_run(self, run_id: str, error: str) -> None:
        run = await self._s.get(StageRun, run_id)
        assert run is not None
        run.status, run.error = "failed", error
        await self._s.flush()

    async def runs_for_document(self, doc_id: str) -> dict[str, RunFacts]:
        rows = (
            await self._s.execute(
                sqlalchemy.select(StageRun)
                .where(StageRun.doc_id == doc_id, StageRun.status == "done")
                .order_by(StageRun.created_at)
            )
        ).scalars().all()
        facts: dict[str, RunFacts] = {}
        for r in rows:  # later done-run for a stage overwrites the earlier
            facts[r.stage_name] = RunFacts(r.stage_version, r.input_hash, r.output_hash or "")
        return facts

    async def latest_run(self, doc_id: str, stage_name: str) -> StageRun | None:
        return (
            await self._s.execute(
                sqlalchemy.select(StageRun)
                .where(StageRun.doc_id == doc_id, StageRun.stage_name == stage_name)
                .order_by(StageRun.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
