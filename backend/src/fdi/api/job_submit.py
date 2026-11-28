"""Admitting, persisting and publishing an API job (T5, T7).

Shared by every route that queues expensive work -- the unversioned routes
(`fdi.api.routes`) and the `/api/v1/` resources (Extraction runs) -- so there
is exactly one admission path: one rate limit, one refund rule, one place
the job row becomes visible before its message is published.
"""

import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.api import limits as ratelimit
from fdi.api.schemas import JobAccepted
from fdi.pipeline.jobs import JobDispatcher
from fdi.platform.db.repository import JobRepository

log = structlog.get_logger(__name__)


async def submit_job(
    session: AsyncSession,
    dispatcher: JobDispatcher,
    limits: ratelimit.RateLimits,
    response: Response,
    *,
    user_id: str,
    kind: str,
    params: dict[str, object],
    correlation_id: str,
    before_commit: Callable[[str], Awaitable[None]] | None = None,
) -> JobAccepted:
    """Admit (T7), persist, THEN publish: a worker that wins the race must
    find the row. The submitting request's correlation id becomes the job's,
    so the 202 the caller holds names every log line the worker writes.

    Admission holds the caller's submission lock until the commit below, so
    the row is visible before the next submission counts jobs in flight.

    `before_commit(job_id)` runs inside the same transaction, after the job
    row exists -- so a row that points at the job (an Extraction run) commits
    atomically with it, and the worker can never see one without the other."""
    quota = await ratelimit.admit_job(session, limits, user_id)
    job_id = str(uuid.uuid4())
    try:
        await JobRepository(session).create(
            job_id=job_id, user_id=user_id, kind=kind, params=params,
            correlation_id=correlation_id,
        )
        if before_commit is not None:
            await before_commit(job_id)
        await session.commit()
        try:
            dispatcher.dispatch(job_id, correlation_id)
        except Exception:
            # No message means no worker will ever touch this row: fail it
            # now, or it sits `queued` in the caller's job list forever (and
            # holds their concurrency slot). The publish error itself still
            # propagates as the 500.
            await JobRepository(session).mark_failed(job_id, "the job could not be queued")
            await session.commit()
            raise
    except Exception:
        # Admitted but never queued: nothing will be spent, so charge nothing.
        await ratelimit.refund(limits, ratelimit.JOBS, user_id)
        raise
    log.info("job.queued", job_id=job_id, kind=kind)
    ratelimit.set_quota_headers(response, quota)
    return JobAccepted(job_id=job_id, correlation_id=correlation_id, status="queued")
