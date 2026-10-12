"""Per-Invitee rate limits, checked when work is submitted (productionization T7).

Two meters protect Azure spend in the Gated demo:

  - Daily quotas (Redis, `fdi.platform.ratelimit.DailyQuota`): one for
    submissions to the four expensive job routes -- which SHARE it, because
    what is being bounded is one person's Azure spend, not traffic to a URL --
    and one for uploads.
  - A cap on expensive jobs in flight (queued or running), counted from
    `api_job` itself, which is the record of what is actually running. A
    second counter in Redis could only ever disagree with it.

Order matters, and is part of the contract: a request refused for any reason
-- 404/409/503 at submit, the concurrency cap -- spends no quota. Only a job
that is admitted is charged, and if it then cannot be queued it is refunded.

What the caller sees:
  - every metered success carries `RateLimit-Limit`, `RateLimit-Remaining`
    and `RateLimit-Reset` (seconds until the window resets), the field names
    of the IETF httpapi RateLimit-header draft;
  - every refusal is a 429 problem+json whose `reason` is `daily_quota` or
    `concurrency`; a quota refusal adds `limit`, `remaining`, `reset_seconds`
    and a `Retry-After`, a concurrency refusal adds `limit` and
    `active_job_ids` -- what to wait for, since no clock can say when.
"""

import contextlib
from dataclasses import dataclass

import structlog
from fastapi import HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.platform.db.repository import JobRepository
from fdi.platform.ratelimit import DailyQuota, QuotaState

log = structlog.get_logger(__name__)

JOBS = "jobs"
UPLOADS = "uploads"

# Headers a cross-origin browser may read: CORSMiddleware's expose_headers.
EXPOSED_HEADERS = ["RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "Retry-After"]


@dataclass(frozen=True)
class RateLimits:
    quota: DailyQuota
    jobs_per_day: int
    uploads_per_day: int
    max_concurrent_jobs: int
    stale_job_seconds: int


def _quota_headers(state: QuotaState) -> dict[str, str]:
    return {
        "RateLimit-Limit": str(state.limit),
        "RateLimit-Remaining": str(state.remaining),
        "RateLimit-Reset": str(state.reset_seconds),
    }


def set_quota_headers(response: Response, state: QuotaState) -> None:
    """Put the caller's remaining quota on a metered success."""
    response.headers.update(_quota_headers(state))


def _quota_exceeded(what: str, state: QuotaState) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "message": (
                f"Daily limit reached: {state.limit} {what} per day. "
                f"It resets in {state.reset_seconds} seconds."
            ),
            "reason": "daily_quota",
            "limit": state.limit,
            "remaining": state.remaining,
            "reset_seconds": state.reset_seconds,
        },
        headers={**_quota_headers(state), "Retry-After": str(state.reset_seconds)},
    )


async def admit_job(session: AsyncSession, limits: RateLimits, user_id: str) -> QuotaState:
    """Admit one expensive job for `user_id`, or raise the 429.

    Takes the user's submission lock in `session`'s transaction, so the
    caller must create the job row and commit in that SAME transaction: the
    commit is what releases the lock, after the new row is visible to the
    next submission's count."""
    repo = JobRepository(session)
    await repo.lock_submissions(user_id)
    active = await repo.in_flight_ids(user_id, stale_after_seconds=limits.stale_job_seconds)
    if len(active) >= limits.max_concurrent_jobs:
        await session.rollback()
        raise HTTPException(
            status_code=429,
            detail={
                "message": (
                    f"You already have {len(active)} job(s) in progress; at most "
                    f"{limits.max_concurrent_jobs} may run at once. Wait for it to "
                    "finish, then submit again."
                ),
                "reason": "concurrency",
                "limit": limits.max_concurrent_jobs,
                "active_job_ids": active,
            },
        )
    state = await limits.quota.consume(JOBS, user_id, limits.jobs_per_day)
    if not state.allowed:
        await session.rollback()
        raise _quota_exceeded("expensive jobs", state)
    return state


async def admit_upload(limits: RateLimits, user_id: str) -> QuotaState:
    state = await limits.quota.consume(UPLOADS, user_id, limits.uploads_per_day)
    if not state.allowed:
        raise _quota_exceeded("uploads", state)
    return state


async def refund(limits: RateLimits, bucket: str, user_id: str) -> None:
    """Return a unit for admitted work that never happened. Best-effort: it
    runs while another exception is propagating, and must not replace it."""
    try:
        await limits.quota.refund(bucket, user_id)
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            log.warning("ratelimit.refund_failed", bucket=bucket, user_id=user_id, error=repr(exc))
