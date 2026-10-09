"""Outbound rate limiting: one async token bucket per external provider.

The bottleneck for pre-warming is not CPU but the rate limits of Exa,
Anthropic and Yahoo. Every outbound call acquires a token from its provider's
bucket first, so however many jobs run at once, calls leave this process no
faster than the configured rate (`EXA_MAX_RPS`, `ANTHROPIC_MAX_RPM`,
`YFINANCE_MAX_RPS`), with bursts up to one second's worth.

The buckets are per process
---------------------------
Each worker process (and the API process, for `wait=true` requests) has its
own buckets and knows nothing of the others. With N processes calling a
provider, configure each with limit / N. The upgrade path, when that becomes
awkward, is a shared bucket: a row per provider in Postgres updated with the
same conditional-UPDATE pattern as the queue, or a Redis token bucket. Neither
is built; at one or two workers the arithmetic is easy.

Backing off
-----------
When a provider answers 429 anyway (another tenant of the same key, a limit
lower than configured), the caller reports it with `backoff`, which stops the
bucket handing out tokens for that long. Every concurrent call to that
provider in this process then waits, instead of each discovering the 429 for
itself. Retrying the failed call is not this module's job: the provider's own
short retries handle a blip, and the job queue's backoff handles the rest.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

SECONDS_PER_MINUTE = 60.0
TOKEN_EPSILON = 1e-9


class TokenBucket:
    """Classic token bucket: `rate` tokens per second, holding at most `capacity`."""

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
        # One second's worth, but never less than one token, or a rate under
        # 1/s could never be served at all.
        self.capacity = capacity if capacity is not None else max(rate, 1.0)
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
            logger.warning("rate_limit_backoff", provider=self.name, seconds=round(seconds, 1))


_buckets: dict[str, TokenBucket] = {}


def bucket(provider: str) -> TokenBucket:
    """The process-wide bucket for `provider` ("exa", "anthropic", "yfinance")."""
    if provider not in _buckets:
        rates = {
            "exa": settings.exa_max_rps,
            "anthropic": settings.anthropic_max_rpm / SECONDS_PER_MINUTE,
            "yfinance": settings.yfinance_max_rps,
        }
        _buckets[provider] = TokenBucket(provider, rates[provider])
    return _buckets[provider]


def reset() -> None:
    """Forget every bucket, e.g. after changing the settings in a test."""
    _buckets.clear()


# Used when a 429 carries no usable Retry-After.
DEFAULT_BACKOFF_SECONDS = 10.0


def retry_after_seconds(value: str | None) -> float:
    """Parse a Retry-After header given in seconds; fall back to a default."""
    try:
        seconds = float(value) if value is not None else DEFAULT_BACKOFF_SECONDS
    except ValueError:
        seconds = DEFAULT_BACKOFF_SECONDS
    return max(seconds, 0.0)
