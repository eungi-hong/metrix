"""Outbound rate limiting: one limiter per external provider, shared by every process.

The bottleneck for pre-warming is not CPU but the rate limits of Exa,
Anthropic and Yahoo, and those limits belong to the account (or, for Yahoo,
the IP), not to a process. So the limits here are shared through Redis by
the API and every worker: `EXA_MAX_RPS`, `ANTHROPIC_MAX_RPM` and
`YFINANCE_MAX_RPS` are what the whole deployment may send. Every outbound call
goes through `await bucket(provider).acquire()` first.

Reserve a slot, then wait for it
--------------------------------
Each provider's limit is a GCRA schedule in Redis (see `app.services.limits`),
allowing bursts of up to one second's worth. `acquire` does not poll: one Lua
script reserves the caller's slot, the next free one, and returns how long
until it, and the caller sleeps exactly that long. Callers in every process
are therefore served in the order they asked, without a thundering herd when
the limit frees up. A slot further away than the caller may wait is not
reserved, and the caller gets `ProviderBusy` instead: `RATE_LIMIT_MAX_WAIT_SECONDS`
for background work, after which the queue backs the job off, and the much
shorter `RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS` for a user, who gets 503 and
`Retry-After`.

A reserve for users
-------------------
Background calls (the call class in `app.core.context`) take a slot from a
`{provider}:background` schedule running at `BACKGROUND_RATE_SHARE` of the
rate, and then one from the provider's own schedule; interactive calls take
only the latter. However much nightly work is queued, it reaches the provider
at most at its share of the rate, and chat always finds headroom. (Why two
steps rather than one atomic reservation: see `ProviderLimiter.acquire`.)

Backing off together
--------------------
When a provider answers 429 anyway (another tenant of the same key, a limit
lower than configured), the caller reports it with `backoff`, which writes
`metrix:ratelimit:{provider}:blocked_until` to Redis. Every process honours
it on its next reservation, instead of each discovering the 429 for itself.
`backoff` stays synchronous for its callers: it blocks this process at once
and writes to Redis in the background. Retrying the failed call is not this
module's job: the provider's own short retries handle a blip, and the job
queue's backoff handles the rest.

When Redis is down
------------------
Each process falls back to its own `TokenBucket`s, at rate / EXPECTED_PROCESSES
(and the background share of that), so together the processes stay near the
account's limit; `limiter_fallback` is logged. The buckets are kept for
exactly this.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

import redis.asyncio as aioredis

from app.core import context
from app.core.config import settings
from app.core.context import CallClass
from app.core.errors import ProviderBusy
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.services.limits import REDIS_ERRORS, Circuit

logger = get_logger(__name__)

SECONDS_PER_MINUTE = 60.0
TOKEN_EPSILON = 1e-9
KEY_PREFIX = "metrix:ratelimit:"
PROVIDERS = ("exa", "anthropic", "yfinance")


def provider_rate(provider: str) -> float:
    """The account-wide rate for `provider`, in requests per second."""
    rates = {
        "exa": settings.exa_max_rps,
        "anthropic": settings.anthropic_max_rpm / SECONDS_PER_MINUTE,
        "yfinance": settings.yfinance_max_rps,
    }
    return rates[provider]


def burst(rate: float) -> float:
    """One second's worth, but never less than one call, or a rate under 1/s
    could never be served at all."""
    return max(rate, 1.0)


# ------------------------------------------------------- per-process fallback


class TokenBucket:
    """Classic token bucket: `rate` tokens per second, holding at most `capacity`.

    The per-process fallback while Redis is unreachable.
    """

    def __init__(
        self,
        name: str,
        rate: float,
        capacity: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.name = name
        self.rate = rate
        self.capacity = capacity if capacity is not None else burst(rate)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._updated = clock()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def wait_time(self) -> float:
        """Seconds until a token is available (0 if one is available now)."""
        now = self._clock()
        self._refill(now)
        blocked = max(self._blocked_until - now, 0.0)
        # A shortfall below TOKEN_EPSILON is float rounding from the refill
        # arithmetic, not a real deficit. Waiting it out would ask for a sleep
        # smaller than the clock can represent, and never get anywhere.
        shortfall = 1.0 - self._tokens
        short = shortfall / self.rate if shortfall > TOKEN_EPSILON else 0.0
        return max(blocked, short)

    def take(self) -> None:
        """Take a token now, going into debt if need be: the reservation a
        caller then waits out (`wait_time` before taking says how long)."""
        self._tokens -= 1.0

    async def acquire(self) -> None:
        """Take one token, waiting as long as needed. Fair in arrival order."""
        async with self._lock:
            while (delay := self.wait_time()) > 0:
                await self._sleep(delay)
            self._tokens -= 1.0

    def backoff(self, seconds: float) -> None:
        """Hand out no tokens for `seconds`: the provider said slow down."""
        until = self._clock() + seconds
        if until > self._blocked_until:
            self._blocked_until = until
            self._tokens = 0.0


# ------------------------------------------------------------ shared stores


class RateStore(Protocol):
    async def reserve(
        self, schedule_key: str, *, provider: str, rate: float, max_wait: float
    ) -> tuple[bool, float]:
        """Reserve the next slot on `schedule_key` within `max_wait` seconds:
        (True, seconds until it). (False, seconds) if it is further than
        that; nothing is reserved then. `provider`'s shared block applies."""
        ...

    async def block(self, provider: str, seconds: float) -> None:
        """No slot for `provider` before `seconds` from now, in any process."""
        ...


def schedule(
    tat: float | None, now: float, blocked: float, *, rate: float
) -> tuple[float, float]:
    """One reservation on a GCRA schedule: (the new TAT, seconds until the slot).

    Pure, so it is tested directly and the Lua script mirrors it. A block
    moves the start of the schedule to its end, so reservations made while
    blocked are spread out after it rather than all released at once.
    """
    interval = 1.0 / rate
    period = burst(rate) * interval
    start = max(now, blocked)
    current = max(tat if tat is not None else start, start)
    advanced = current + interval
    slot = max(advanced - period, blocked, now)
    return advanced, slot - now


class MemoryRateStore:
    """A shared store in one process: two `ProviderLimiter`s on one instance
    behave like two processes on one Redis. For tests."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._tats: dict[str, float] = {}
        self._blocked: dict[str, float] = {}

    async def reserve(self, schedule_key, *, provider, rate, max_wait):
        now = self._clock()
        tat, wait = schedule(
            self._tats.get(schedule_key), now, self._blocked.get(provider, 0.0), rate=rate
        )
        if wait > max_wait + TOKEN_EPSILON:
            return False, wait
        self._tats[schedule_key] = tat
        return True, wait

    async def block(self, provider: str, seconds: float) -> None:
        until = self._clock() + seconds
        self._blocked[provider] = max(self._blocked.get(provider, 0.0), until)


# Mirrors `schedule`. KEYS: the schedule, the provider's blocked_until.
# ARGV: rate, max wait.
_RESERVE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local blocked = tonumber(redis.call('GET', KEYS[2])) or 0
local rate = tonumber(ARGV[1])
local interval = 1 / rate
local period = math.max(rate, 1) * interval
local start = math.max(now, blocked)
local tat = tonumber(redis.call('GET', KEYS[1])) or start
if tat < start then tat = start end
local advanced = tat + interval
local wait = math.max(advanced - period, blocked, now) - now
if wait > tonumber(ARGV[2]) + 1e-9 then return {0, tostring(wait)} end
redis.call('SET', KEYS[1], tostring(advanced), 'PX', math.ceil((advanced - now) * 1000) + 1)
return {1, tostring(wait)}
"""

# Raise blocked_until, never lower it: a shorter backoff must not cut a longer one short.
_BLOCK_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local until_at = now + tonumber(ARGV[1])
local current = tonumber(redis.call('GET', KEYS[1])) or 0
if until_at > current then
  redis.call('SET', KEYS[1], tostring(until_at), 'PX', math.ceil(tonumber(ARGV[1]) * 1000) + 1)
end
return 1
"""


class RedisRateStore:
    def __init__(self, client: aioredis.Redis) -> None:
        self._reserve = client.register_script(_RESERVE_LUA)
        self._block = client.register_script(_BLOCK_LUA)

    async def reserve(self, schedule_key, *, provider, rate, max_wait):
        granted, wait = await self._reserve(
            keys=[KEY_PREFIX + schedule_key, f"{KEY_PREFIX}{provider}:blocked_until"],
            args=[rate, max_wait],
        )
        return bool(int(granted)), float(wait)

    async def block(self, provider: str, seconds: float) -> None:
        await self._block(keys=[f"{KEY_PREFIX}{provider}:blocked_until"], args=[seconds])


# ----------------------------------------------------------- the limiter


class ProviderLimiter:
    """What `bucket(provider)` returns: shared when it can be, per process when not."""

    def __init__(
        self,
        name: str,
        store: RateStore | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.name = name
        self.rate = provider_rate(name)
        self.background_rate = self.rate * settings.background_rate_share
        self.store = store
        self._clock = clock
        self._sleep = sleep
        self._circuit = Circuit(f"ratelimit:{name}", clock)
        self._blocked_until = 0.0  # this process's own view, set at once by backoff
        # Rate / processes: the processes' fallbacks together stay near the limit.
        share = 1 / settings.expected_processes
        self._fallback = TokenBucket(name, self.rate * share, clock=clock)
        self._fallback_background = TokenBucket(
            f"{name}:background", max(self.background_rate * share, TOKEN_EPSILON), clock=clock
        )
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def degraded(self) -> bool:
        return self.store is not None and not self._circuit.closed

    async def acquire(self) -> None:
        """Wait for this call's slot, or raise `ProviderBusy` if it is further
        away than the caller may wait.

        A background call first waits for its slot on the background schedule
        (`BACKGROUND_RATE_SHARE` of the rate), and only then reserves on the
        provider's schedule, where it competes with interactive calls as an
        equal. Two steps, not one: a GCRA schedule is a single time and holds
        no gaps, so a background call reserving a future provider slot at once
        would push every interactive call behind the background backlog, the
        opposite of a reserve. Paced first, background traffic reaches the
        provider's schedule at most at its share, and can never build that
        backlog.
        """
        background = context.current_call_class.get() == CallClass.BACKGROUND
        max_wait = (
            settings.rate_limit_max_wait_seconds
            if background
            else settings.rate_limit_interactive_max_wait_seconds
        )
        deadline = self._clock() + max_wait
        if background:
            await self._take(f"{self.name}:background", self.background_rate, deadline, background)
        await self._take(self.name, self.rate, deadline, background)

    async def _take(self, key: str, rate: float, deadline: float, background: bool) -> None:
        max_wait = max(deadline - self._clock(), 0.0)
        granted, wait = await self._reserve(key, rate, max_wait)
        if not granted:
            logger.warning(
                "rate_limit_wait_exceeded",
                provider=self.name,
                schedule=key,
                call_class="background" if background else "interactive",
                wait_s=round(wait, 1),
                max_wait_s=round(max_wait, 1),
            )
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=wait)
            raise ProviderBusy(
                self.name,
                retry_at,
                f"{self.name} is at its rate limit; the next slot is {math.ceil(wait)} s away.",
            )
        if wait > 0:
            await self._sleep(wait)

    async def _reserve(self, key: str, rate: float, max_wait: float) -> tuple[bool, float]:
        local_block = max(self._blocked_until - self._clock(), 0.0)
        if local_block > max_wait + TOKEN_EPSILON:
            return False, local_block
        if self.store is not None and self._circuit.closed:
            try:
                granted, wait = await self.store.reserve(
                    key, provider=self.name, rate=rate, max_wait=max_wait - local_block
                )
                return granted, wait + local_block
            except REDIS_ERRORS as exc:
                self._circuit.fail("reserve", exc)
        fallback = self._fallback_background if key.endswith(":background") else self._fallback
        wait = fallback.wait_time()
        if wait > max_wait + TOKEN_EPSILON:
            return False, wait
        fallback.take()
        return True, wait

    def backoff(self, seconds: float) -> None:
        """The provider said slow down: stop this process at once, and every
        other process as soon as the shared block is written."""
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)
        self._fallback.backoff(seconds)
        self._fallback_background.backoff(seconds)
        logger.warning("rate_limit_backoff", provider=self.name, seconds=round(seconds, 1))
        if self.store is not None and self._circuit.closed:
            task = asyncio.get_running_loop().create_task(self._share_block(seconds))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _share_block(self, seconds: float) -> None:
        try:
            await self.store.block(self.name, seconds)  # type: ignore[union-attr]
        except REDIS_ERRORS as exc:
            self._circuit.fail("block", exc)

    async def settle(self) -> None:
        """Wait for background writes of `backoff` to finish. For tests and shutdown."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)


_limiters: dict[str, ProviderLimiter] = {}


def bucket(provider: str) -> ProviderLimiter:
    """The process's limiter for `provider` ("exa", "anthropic", "yfinance")."""
    if provider not in _limiters:
        client = get_redis()
        store = RedisRateStore(client) if client is not None else None
        _limiters[provider] = ProviderLimiter(provider, store)
    return _limiters[provider]


def reset() -> None:
    """Forget every limiter, e.g. after changing the settings in a test."""
    _limiters.clear()


# Used when a 429 carries no usable Retry-After.
DEFAULT_BACKOFF_SECONDS = 10.0


def retry_after_seconds(value: str | None) -> float:
    """Parse a Retry-After header given in seconds; fall back to a default."""
    try:
        seconds = float(value) if value is not None else DEFAULT_BACKOFF_SECONDS
    except ValueError:
        seconds = DEFAULT_BACKOFF_SECONDS
    return max(seconds, 0.0)
