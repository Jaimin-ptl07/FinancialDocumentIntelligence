"""Job persistence. The caller owns the transaction — commit is theirs, not ours.

This class is the sole writer of `jobs`, and `Job.updated_at` depends on that
staying true: `onupdate=func.now()` is a SQLAlchemy-level default, so it only
fires for writes this ORM builds. The mutators below deliberately mutate loaded
ORM attributes — that path emits `SET ..., updated_at=now()`. Reaching for
`ON CONFLICT DO UPDATE` here would silently stop `updated_at` advancing (the
upsert emits no `updated_at` in its SET clause) and every staleness check built
on the column would quietly start lying. See the note on `models.Job.updated_at`.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.platform.db.models import Job, JobStatus


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
