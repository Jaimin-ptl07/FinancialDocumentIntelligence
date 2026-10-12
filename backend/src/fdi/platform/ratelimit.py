"""Per-Invitee daily quotas: fixed-window counters in Redis (productionization T7).

One key per (bucket, user, window): `fdi:quota:<bucket>:<user>:<window_s>:<n>`,
where n = floor(now / window_s). Windows are aligned to the epoch, so with the
default 86400 s every quota resets together at 00:00 UTC -- one reset time an
operator can state, rather than one per user per first request. The key's own
TTL outlives its window by a margin and then Redis deletes it; nothing sweeps.
A fixed window, not a sliding one: an Invitee can spend a full quota just
before the reset and another just after -- 2x the cap across midnight, still
bounded, and simpler to explain than a sliding log.

Check-and-increment is one Lua script, so two concurrent requests cannot both
see `limit - 1` and both pass. A refused request never increments: a caller
retrying past the cap does not push their own reset further away, and
`remaining` never goes negative.
"""

import math
import time
from dataclasses import dataclass

from redis.asyncio import Redis

# KEYS[1] counter; ARGV[1] limit, ARGV[2] ttl ms. Returns {allowed, count}.
_CONSUME = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count >= tonumber(ARGV[1]) then
  return {0, count}
end
count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return {1, count}
"""

# Never below zero: a refund for a window that already rolled over (or whose
# key expired) must not hand out credit in the next one.
_REFUND = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count > 0 then
  return redis.call('DECR', KEYS[1])
end
return 0
"""

_TTL_MARGIN_MS = 60_000


@dataclass(frozen=True)
class QuotaState:
    """One quota decision, in the units the caller is told: `reset_seconds`
    is a whole number of seconds until the window rolls over, at least 1."""

    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


class DailyQuota:
    def __init__(self, redis: Redis, *, window_seconds: int) -> None:
        self._redis = redis
        self._window = window_seconds

    def _key(self, bucket: str, user_id: str, now: float) -> str:
        n = math.floor(now / self._window)
        return f"fdi:quota:{bucket}:{user_id}:{self._window}:{n}"

    async def consume(self, bucket: str, user_id: str, limit: int) -> QuotaState:
        """Spend one unit of `user_id`'s `bucket` quota if any is left."""
        now = time.time()
        window_end = (math.floor(now / self._window) + 1) * self._window
        ttl_ms = math.ceil((window_end - now) * 1000) + _TTL_MARGIN_MS
        allowed, count = await self._redis.eval(
            _CONSUME, 1, self._key(bucket, user_id, now), limit, ttl_ms
        )
        return QuotaState(
            allowed=bool(allowed),
            limit=limit,
            remaining=max(limit - int(count), 0),
            reset_seconds=max(math.ceil(window_end - now), 1),
        )

    async def refund(self, bucket: str, user_id: str) -> None:
        """Give back one unit spent in the CURRENT window -- for work that was
        admitted but never happened (the job could not be queued)."""
        await self._redis.eval(
            _REFUND, 1, self._key(bucket, user_id, time.time())
        )
