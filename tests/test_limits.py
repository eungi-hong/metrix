"""Tests for the limiter: the GCRA math on a fake clock, the store contract
(run against memory, and against real Redis when TEST_REDIS_URL is set), and
the fall-back to per-process limits when Redis is unreachable.

    TEST_REDIS_URL=redis://localhost:6380/15 pytest -m redis

The Redis database is flushed before each test, so point it at a spare one.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone

import pytest
import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import settings
from app.services import limits
from app.services.limits import (
    GcraStep,
    Limiter,
    MemoryLimitStore,
    RedisLimitStore,
    gcra_result,
    gcra_step,
)

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
REDIS = [
    pytest.mark.redis,
    pytest.mark.skipif(not TEST_REDIS_URL, reason="TEST_REDIS_URL is not set"),
]


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------- the math


def run(store_clock: FakeClock, tat: float | None, *, limit=5, period=60.0, cost=1):
    step = gcra_step(tat, store_clock.now, limit=limit, period=period, cost=cost)
    return step, gcra_result(step, store_clock.now, limit=limit, period=period, cost=cost)


def test_a_burst_up_to_the_limit_then_exactly_the_rate():
    clock = FakeClock()
    tat = None
    remaining = []
    for _ in range(5):
        step, result = run(clock, tat)
        assert result.allowed
        tat = step.tat
        remaining.append(result.remaining)
    assert remaining == [4, 3, 2, 1, 0]

    step, refused = run(clock, tat)
    assert not refused.allowed and step.tat == tat, "a refusal changes nothing"
    assert refused.retry_after == pytest.approx(12.0)  # 60 s / 5
    assert refused.reset_at == datetime.fromtimestamp(clock.now + 60, timezone.utc)

    clock.now += 11.9
    assert not run(clock, tat)[1].allowed
    clock.now += 0.1
    step, result = run(clock, tat)
    assert result.allowed and result.remaining == 0


def test_an_idle_key_is_fully_rested_and_never_banks_more():
    clock = FakeClock()
    step, _ = run(clock, None)
    clock.now += 3600
    _, result = run(clock, step.tat)
    assert result.remaining == 4, "rested to the full limit, not beyond"


def test_a_costly_request_takes_several_cells():
    clock = FakeClock()
    step, result = run(clock, None, cost=3)
    assert result.allowed and result.remaining == 2
    _, refused = run(clock, step.tat, cost=3)
    assert not refused.allowed
    assert refused.retry_after == pytest.approx(12.0)  # needs one more cell back


def test_a_zero_limit_refuses_everything():
    clock = FakeClock()
    _, result = run(clock, None, limit=0)
    assert not result.allowed and result.remaining == 0 and result.retry_after == 60.0


def test_headers_carry_seconds_until_reset():
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    result = gcra_result(GcraStep(True, now.timestamp() + 24.2), now.timestamp(), limit=5, period=60, cost=1)
    assert result.headers(now) == {
        "X-RateLimit-Limit": "5",
        "X-RateLimit-Remaining": "2",  # (60 - 24.2) // 12
        "X-RateLimit-Reset": "25",
    }


async def test_daily_counts_reset_with_the_utc_date():
    clock = FakeClock(datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc).timestamp())
    store = MemoryLimitStore(clock)
    today = date(2026, 10, 9)
    for _ in range(3):
        assert (await store.daily("k", limit=3, day=today)).allowed
    refused = await store.daily("k", limit=3, day=today)
    assert not refused.allowed and refused.remaining == 0
    assert refused.retry_after == pytest.approx(60.0)
    assert refused.reset_at == datetime(2026, 10, 10, tzinfo=timezone.utc)

    clock.now += 120
    assert (await store.daily("k", limit=3, day=date(2026, 10, 10))).remaining == 2


# ------------------------------------------------------- the contract


@pytest.fixture(params=["memory", pytest.param("redis", marks=REDIS)])
async def store(request) -> AsyncIterator[limits.LimitStore]:
    if request.param == "memory":
        yield MemoryLimitStore()
        return
    client = aioredis.from_url(TEST_REDIS_URL)
    await client.flushdb()
    yield RedisLimitStore(client)
    await client.flushdb()
    await client.aclose()


async def test_contract_per_minute(store):
    results = [await store.gcra("rpm:alice", limit=5, period=60) for _ in range(6)]

    assert [r.allowed for r in results] == [True] * 5 + [False]
    assert [r.remaining for r in results[:5]] == [4, 3, 2, 1, 0]
    assert results[5].retry_after == pytest.approx(12.0, abs=0.5)
    assert (await store.gcra("rpm:bob", limit=5, period=60)).allowed, "keys are separate"


async def test_contract_per_minute_refund(store):
    for _ in range(5):
        await store.gcra("rpm:alice", limit=5, period=60)
    await store.gcra_refund("rpm:alice", limit=5, period=60)
    assert (await store.gcra("rpm:alice", limit=5, period=60)).allowed
    assert not (await store.gcra("rpm:alice", limit=5, period=60)).allowed


async def test_contract_daily(store):
    day = datetime.now(timezone.utc).date()
    results = [await store.daily("chat:alice", limit=3, day=day) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0]

    await store.daily_refund("chat:alice", day=day)
    assert (await store.daily("chat:alice", limit=3, day=day)).allowed
    assert (await store.daily("chat:alice", limit=3, day=day + timedelta(days=1))).remaining == 2


async def test_contract_a_refused_daily_charge_costs_nothing(store):
    day = datetime.now(timezone.utc).date()
    await store.daily("k", limit=3, day=day, cost=2)
    assert not (await store.daily("k", limit=3, day=day, cost=2)).allowed
    assert (await store.daily("k", limit=3, day=day, cost=1)).allowed


async def test_contract_first_seen(store):
    assert await store.first_seen("demand:alice:AAPL", ttl=60)
    assert not await store.first_seen("demand:alice:AAPL", ttl=60)
    assert await store.first_seen("demand:bob:AAPL", ttl=60)


# ----------------------------------------------------------- fall-back


class BrokenStore:
    """Fails like Redis does when it is down."""

    def __init__(self) -> None:
        self.calls = 0

    async def gcra(self, *args, **kwargs):
        self.calls += 1
        raise RedisConnectionError("Connection refused")

    gcra_refund = daily = daily_refund = first_seen = gcra


@pytest.fixture
def logged(monkeypatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(limits.logger, "warning", lambda event, **f: events.append((event, f)))
    return events


async def test_when_redis_fails_limits_continue_per_process(logged, monkeypatch):
    monkeypatch.setattr(settings, "redis_retry_seconds", 5.0)
    clock = FakeClock()
    broken = BrokenStore()
    limiter = Limiter(broken, MemoryLimitStore(), clock=clock)

    results = [await limiter.gcra("rpm:alice", limit=3, period=60) for _ in range(4)]

    assert [r.allowed for r in results] == [True, True, True, False], "still bounded"
    assert broken.calls == 1, "Redis is not retried on every call while it is down"
    assert limiter.degraded
    assert [event for event, _ in logged] == ["limiter_fallback"]

    clock.now += 5.0
    await limiter.gcra("rpm:alice", limit=3, period=60)
    assert broken.calls == 2, "tried again after REDIS_RETRY_SECONDS"


async def test_the_fallback_is_logged_at_most_once_a_minute(logged, monkeypatch):
    monkeypatch.setattr(settings, "redis_retry_seconds", 1.0)
    monkeypatch.setattr(settings, "limiter_fallback_log_seconds", 60.0)
    clock = FakeClock()
    limiter = Limiter(BrokenStore(), MemoryLimitStore(), clock=clock)
    for _ in range(10):
        await limiter.gcra("k", limit=100, period=60)
        clock.now += 2.0
    assert len(logged) == 1
    clock.now += 60
    await limiter.gcra("k", limit=100, period=60)
    assert len(logged) == 2


async def test_an_unreachable_redis_falls_back_for_real(logged, monkeypatch):
    """The real client, against a port nothing listens on."""
    monkeypatch.setattr(settings, "redis_timeout_seconds", 0.2)
    client = aioredis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2)
    limiter = Limiter(RedisLimitStore(client), MemoryLimitStore())

    result = await limiter.daily("k", limit=2, day=date.today())

    assert result.allowed and result.remaining == 1
    assert logged and logged[0][0] == "limiter_fallback"
    await client.aclose()


def test_without_redis_the_limiter_is_per_process(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)
    limits.configure(None)
    limiter = limits.get_limiter()
    assert limiter.primary is None and not limiter.degraded
