"""Rate limiting primitives: per-minute limits (GCRA) and daily counters.

What the limits are for, and per plan, lives in `app.services.quotas`; this
module is only the mechanics, behind a small `LimitStore` protocol with two
implementations. `RedisLimitStore` is shared by every process. `MemoryLimitStore`
is per process: it backs the tests, and stands in while Redis is unreachable.

Per-minute limits: GCRA
-----------------------
The generic cell rate algorithm keeps one number per key, the "theoretical
arrival time" (TAT): when the key would be fully rested if requests had
arrived exactly at the allowed rate. With `limit` requests per `period`, each
request moves the TAT forward by `period / limit`, and a request is refused if
that would put the TAT more than `period` ahead of now. This allows a burst of
up to `limit` at once, then exactly the sustained rate.

GCRA rather than a sliding-window log: the log stores a timestamp per request
(memory grows with the limit, and every check trims and counts it), while GCRA
is one key with one number, updated in one atomic Lua script, in constant time
and memory. And it knows exactly when the next request will fit, so
`Retry-After` is exact rather than "try again when the window rolls over".
The script reads the time from Redis itself, so processes whose clocks
disagree still agree on the limit.

Daily quotas: counters
----------------------
`INCRBY` on a key named for the UTC date, expiring a little after midnight,
checked and incremented in one script so a refused request costs nothing. A
day is the natural unit for a quota ("50 chats a day"), and resets at a time
a user can be told.

Failure policy
--------------
If Redis is unreachable, limits fail open, but not unbounded: the `Limiter`
logs `limiter_fallback` (at most once per LIMITER_FALLBACK_LOG_SECONDS) and
uses a per-process `MemoryLimitStore` until REDIS_RETRY_SECONDS have passed,
then tries Redis again. Each process then enforces each limit on its own,
so a caller can get up to (processes x limit) for the length of the outage:
approximate, but still bounded. Taking the API down because a limiter is
unavailable would be worse. Money is not at risk either way: the spend cap
lives in Postgres (`app.services.spend`).
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Protocol

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

# A daily key outlives its day by this much, so a request at 23:59:59 and the
# expiry never race; the next day's key has a different name anyway.
DAILY_KEY_GRACE_SECONDS = 3600
# Float slack in comparisons of times computed by different paths.
EPSILON = 1e-9
KEY_PREFIX = "metrix:limit:"


@dataclass(frozen=True, slots=True)
class LimitResult:
    """The outcome of one check, with everything the rate-limit headers need."""

    allowed: bool
    limit: int
    remaining: int
    reset_at: datetime
    # Seconds until a request of the same cost would be allowed; 0 if this one was.
    retry_after: float

    def headers(self, now: datetime | None = None) -> dict[str, str]:
        """X-RateLimit-* headers. Reset is in seconds from now, as in the IETF
        RateLimit header draft, so clients need not trust their own clocks."""
        now = now or datetime.now(timezone.utc)
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(max(math.ceil((self.reset_at - now).total_seconds()), 0)),
        }


class LimitStore(Protocol):
    async def gcra(self, key: str, *, limit: int, period: float, cost: int = 1) -> LimitResult: ...

    async def gcra_refund(self, key: str, *, limit: int, period: float, cost: int = 1) -> None: ...

    async def daily(self, key: str, *, limit: int, day: date, cost: int = 1) -> LimitResult: ...

    async def daily_refund(self, key: str, *, day: date, cost: int = 1) -> None: ...

    async def first_seen(self, key: str, *, ttl: float) -> bool:
        """True the first time `key` is seen within `ttl` seconds (SET NX)."""
        ...


# -------------------------------------------------------------- the math


@dataclass(frozen=True, slots=True)
class GcraStep:
    allowed: bool
    tat: float  # the TAT to store: advanced if allowed, unchanged if not


def gcra_step(tat: float | None, now: float, *, limit: int, period: float, cost: int) -> GcraStep:
    """One GCRA decision. Pure, so the math is tested on a fake clock, and
    the Lua script is a line-for-line copy of it."""
    if limit <= 0:
        return GcraStep(allowed=False, tat=max(tat or now, now))
    interval = period / limit
    current = max(tat if tat is not None else now, now)
    advanced = current + interval * cost
    if advanced - period > now + EPSILON:
        return GcraStep(allowed=False, tat=current)
    return GcraStep(allowed=True, tat=advanced)


def gcra_result(step: GcraStep, now: float, *, limit: int, period: float, cost: int) -> LimitResult:
    """Describe a decision: what is left, when it is fully reset, when to retry."""
    at = datetime.fromtimestamp(now, timezone.utc)
    if limit <= 0:
        return LimitResult(False, 0, 0, at + timedelta(seconds=period), period)
    interval = period / limit
    ahead = step.tat - now  # how far the TAT runs ahead of now
    remaining = max(math.floor((period - ahead) / interval + EPSILON), 0)
    retry_after = 0.0 if step.allowed else max(ahead + interval * cost - period, 0.0)
    return LimitResult(
        allowed=step.allowed,
        limit=limit,
        remaining=remaining,
        reset_at=at + timedelta(seconds=max(ahead, 0.0)),
        retry_after=retry_after,
    )


def day_end(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), dtime.min, tzinfo=timezone.utc)


def daily_result(allowed: bool, used: int, *, limit: int, day: date, now: datetime) -> LimitResult:
    reset_at = day_end(day)
    return LimitResult(
        allowed=allowed,
        limit=limit,
        remaining=max(limit - used, 0),
        reset_at=reset_at,
        retry_after=0.0 if allowed else max((reset_at - now).total_seconds(), 0.0),
    )


def _daily_key(key: str, day: date) -> str:
    return f"{KEY_PREFIX}{key}:{day.isoformat()}"


# ------------------------------------------------------------ in memory


class MemoryLimitStore:
    """Per-process limits. Exact within one process; each check runs without
    an `await` in its critical section, so concurrent tasks cannot interleave."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._tats: dict[str, float] = {}
        self._counts: dict[str, tuple[int, float]] = {}  # key -> (count, expires at)
        self._seen: dict[str, float] = {}  # key -> expires at

    async def gcra(self, key: str, *, limit: int, period: float, cost: int = 1) -> LimitResult:
        now = self._clock()
        step = gcra_step(self._tats.get(key), now, limit=limit, period=period, cost=cost)
        if step.allowed:
            self._tats[key] = step.tat
        return gcra_result(step, now, limit=limit, period=period, cost=cost)

    async def gcra_refund(self, key: str, *, limit: int, period: float, cost: int = 1) -> None:
        if limit <= 0 or key not in self._tats:
            return
        self._tats[key] -= period / limit * cost
        if self._tats[key] <= self._clock():
            del self._tats[key]

    async def daily(self, key: str, *, limit: int, day: date, cost: int = 1) -> LimitResult:
        now = self._clock()
        full_key = _daily_key(key, day)
        used, expires = self._counts.get(full_key, (0, 0.0))
        if expires <= now:
            used = 0
        at = datetime.fromtimestamp(now, timezone.utc)
        if used + cost > limit:
            return daily_result(False, used, limit=limit, day=day, now=at)
        expiry = day_end(day).timestamp() + DAILY_KEY_GRACE_SECONDS
        self._counts[full_key] = (used + cost, expiry)
        return daily_result(True, used + cost, limit=limit, day=day, now=at)

    async def daily_refund(self, key: str, *, day: date, cost: int = 1) -> None:
        full_key = _daily_key(key, day)
        if full_key in self._counts:
            used, expires = self._counts[full_key]
            self._counts[full_key] = (max(used - cost, 0), expires)

    async def first_seen(self, key: str, *, ttl: float) -> bool:
        now = self._clock()
        if self._seen.get(key, 0.0) > now:
            return False
        self._seen[key] = now + ttl
        return True


# ---------------------------------------------------------------- Redis

# Mirrors `gcra_step`. Returns {allowed, tat, now} with the times as strings:
# Redis truncates Lua numbers to integers in replies.
_GCRA_LUA = """
local interval = tonumber(ARGV[1])
local period = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local tat = tonumber(redis.call('GET', KEYS[1]))
if tat == nil or tat < now then tat = now end
local advanced = tat + interval * cost
if advanced - period > now + 1e-9 then
  return {0, tostring(tat), tostring(now)}
end
redis.call('SET', KEYS[1], tostring(advanced), 'PX', math.ceil((advanced - now) * 1000) + 1)
return {1, tostring(advanced), tostring(now)}
"""

_GCRA_REFUND_LUA = """
local tat = tonumber(redis.call('GET', KEYS[1]))
if tat == nil then return 0 end
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local back = tat - tonumber(ARGV[1])
if back <= now then
  redis.call('DEL', KEYS[1])
else
  redis.call('SET', KEYS[1], tostring(back), 'PX', math.ceil((back - now) * 1000) + 1)
end
return 1
"""

# Check and increment in one step, so a refused request does not count.
_DAILY_LUA = """
local used = tonumber(redis.call('GET', KEYS[1])) or 0
local cost = tonumber(ARGV[2])
if used + cost > tonumber(ARGV[1]) then return {0, used} end
used = redis.call('INCRBY', KEYS[1], cost)
redis.call('EXPIREAT', KEYS[1], ARGV[3])
return {1, used}
"""

_DAILY_REFUND_LUA = """
local used = tonumber(redis.call('GET', KEYS[1]))
if used == nil or used <= 0 then return 0 end
return redis.call('DECRBY', KEYS[1], math.min(used, tonumber(ARGV[1])))
"""


class RedisLimitStore:
    """Limits shared by every process. Each operation is one atomic script."""

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client
        self._gcra = client.register_script(_GCRA_LUA)
        self._gcra_refund = client.register_script(_GCRA_REFUND_LUA)
        self._daily = client.register_script(_DAILY_LUA)
        self._daily_refund = client.register_script(_DAILY_REFUND_LUA)

    async def gcra(self, key: str, *, limit: int, period: float, cost: int = 1) -> LimitResult:
        if limit <= 0:
            now = time.time()
            return gcra_result(GcraStep(False, now), now, limit=limit, period=period, cost=cost)
        allowed, tat, now = await self._gcra(
            keys=[KEY_PREFIX + key], args=[period / limit, period, cost]
        )
        step = GcraStep(allowed=bool(int(allowed)), tat=float(tat))
        return gcra_result(step, float(now), limit=limit, period=period, cost=cost)

    async def gcra_refund(self, key: str, *, limit: int, period: float, cost: int = 1) -> None:
        if limit > 0:
            await self._gcra_refund(keys=[KEY_PREFIX + key], args=[period / limit * cost])

    async def daily(self, key: str, *, limit: int, day: date, cost: int = 1) -> LimitResult:
        expire_at = int(day_end(day).timestamp()) + DAILY_KEY_GRACE_SECONDS
        allowed, used = await self._daily(
            keys=[_daily_key(key, day)], args=[limit, cost, expire_at]
        )
        return daily_result(
            bool(int(allowed)), int(used), limit=limit, day=day, now=datetime.now(timezone.utc)
        )

    async def daily_refund(self, key: str, *, day: date, cost: int = 1) -> None:
        await self._daily_refund(keys=[_daily_key(key, day)], args=[cost])

    async def first_seen(self, key: str, *, ttl: float) -> bool:
        return bool(await self._client.set(KEY_PREFIX + key, 1, nx=True, px=math.ceil(ttl * 1000)))


# --------------------------------------------------------- the facade

REDIS_ERRORS = (RedisError, OSError, asyncio.TimeoutError)


class Circuit:
    """Whether to try Redis, for one component that can fall back without it.

    After a failure it stays open (Redis skipped) for REDIS_RETRY_SECONDS, so
    an outage costs one timeout rather than one per call, and logs
    `limiter_fallback` at most once per LIMITER_FALLBACK_LOG_SECONDS.
    """

    def __init__(self, component: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.component = component
        self._clock = clock
        self._down_until = 0.0
        self._last_logged = -math.inf

    @property
    def closed(self) -> bool:
        """True when Redis should be tried."""
        return self._clock() >= self._down_until

    def fail(self, operation: str, exc: BaseException) -> None:
        now = self._clock()
        self._down_until = now + settings.redis_retry_seconds
        if now - self._last_logged >= settings.limiter_fallback_log_seconds:
            self._last_logged = now
            logger.warning(
                "limiter_fallback",
                component=self.component,
                operation=operation,
                error=f"{type(exc).__name__}: {exc}"[:200],
                retry_in_s=settings.redis_retry_seconds,
                detail="Redis unreachable: limits are per process until it is back.",
            )


class Limiter:
    """Redis when it answers, this process's memory when it does not.

    Implements `LimitStore` itself, so callers never see which one served
    them. See the module docstring for why this fails open.
    """

    def __init__(
        self,
        primary: LimitStore | None,
        fallback: MemoryLimitStore | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.primary = primary
        self.fallback = fallback or MemoryLimitStore()
        self._circuit = Circuit("quotas", clock)

    @property
    def degraded(self) -> bool:
        """True while falling back: Redis is configured but failing."""
        return self.primary is not None and not self._circuit.closed

    async def _run(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        if self.primary is not None and self._circuit.closed:
            try:
                return await getattr(self.primary, operation)(*args, **kwargs)
            except REDIS_ERRORS as exc:
                self._circuit.fail(operation, exc)
        return await getattr(self.fallback, operation)(*args, **kwargs)

    async def gcra(self, key: str, *, limit: int, period: float, cost: int = 1) -> LimitResult:
        return await self._run("gcra", key, limit=limit, period=period, cost=cost)

    async def gcra_refund(self, key: str, *, limit: int, period: float, cost: int = 1) -> None:
        await self._run("gcra_refund", key, limit=limit, period=period, cost=cost)

    async def daily(self, key: str, *, limit: int, day: date, cost: int = 1) -> LimitResult:
        return await self._run("daily", key, limit=limit, day=day, cost=cost)

    async def daily_refund(self, key: str, *, day: date, cost: int = 1) -> None:
        await self._run("daily_refund", key, day=day, cost=cost)

    async def first_seen(self, key: str, *, ttl: float) -> bool:
        return await self._run("first_seen", key, ttl=ttl)


_limiter: Limiter | None = None


def get_limiter() -> Limiter:
    """The process's limiter, built from settings on first use."""
    global _limiter
    if _limiter is None:
        client = get_redis()
        _limiter = Limiter(RedisLimitStore(client) if client is not None else None)
        if client is None:
            logger.warning(
                "limiter_without_redis",
                detail="REDIS_URL is unset: limits are per process.",
            )
    return _limiter


def configure(limiter: Limiter | None) -> None:
    """Use `limiter` from now on; None rebuilds from settings on next use."""
    global _limiter
    _limiter = limiter
