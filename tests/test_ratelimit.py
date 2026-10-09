"""Tests for outbound rate limits: the per-process token buckets, on a fake
clock; and the account-wide limits shared through Redis (the schedule math,
the reserve for users, waiting limits, shared backoff, and the fallback),
with a virtual clock and, when TEST_REDIS_URL is set, on real Redis."""

from __future__ import annotations

import asyncio
import heapq
import itertools
import os
from datetime import datetime, timedelta, timezone

import pytest
import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import settings
from app.core.context import CallClass, attributed
from app.core.errors import ProviderBusy
from app.models.enums import IngestStatus, NewsStatus
from app.models.identity import Plan
from app.models.jobs import Job, JobKind, JobSource, JobStatus
from app.models.market import Movement
from app.services import ingestion, limits, queue, ratelimit
from app.services import prices as price_service
from app.services.ratelimit import (
    MemoryRateStore,
    ProviderLimiter,
    RedisRateStore,
    TokenBucket,
    schedule,
)
from app.worker import Worker
from tests.conftest import (
    StubLLM,
    api_app,
    api_client,
    build_price_history,
    new_api_key,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make(name: str, rate: float, clock: FakeClock) -> TokenBucket:
    return TokenBucket(name, rate=rate, clock=clock, sleep=clock.sleep)


async def test_a_burst_up_to_capacity_is_free_then_calls_are_paced(clock):
    bucket = make("exa", 5.0, clock)

    for _ in range(5):
        await bucket.acquire()
    assert clock.slept == [], "one second's worth goes out at once"

    await bucket.acquire()
    await bucket.acquire()
    assert sum(clock.slept) == pytest.approx(2 / 5)


async def test_tokens_refill_while_idle(clock):
    bucket = make("exa", 2.0, clock)
    for _ in range(2):
        await bucket.acquire()

    clock.now += 10  # long idle: refills, but only to capacity
    for _ in range(2):
        await bucket.acquire()
    assert clock.slept == []
    await bucket.acquire()
    assert sum(clock.slept) == pytest.approx(0.5)


async def test_a_slow_rate_still_serves_one_call_at_a_time(clock):
    """50/minute is under one token per second; capacity must still be >= 1."""
    bucket = make("anthropic", 50 / 60, clock)

    await bucket.acquire()
    await bucket.acquire()

    assert sum(clock.slept) == pytest.approx(60 / 50)


async def test_backoff_stops_the_bucket_for_the_given_time(clock):
    bucket = make("exa", 100.0, clock)

    bucket.backoff(7.0)
    await bucket.acquire()

    assert sum(clock.slept) >= 7.0


def test_rates_come_from_settings_per_provider(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "exa_max_rps", 3.0)
    monkeypatch.setattr(settings, "anthropic_max_rpm", 120.0)
    ratelimit.reset()

    assert ratelimit.bucket("exa").rate == 3.0
    assert ratelimit.bucket("anthropic").rate == pytest.approx(2.0)
    assert ratelimit.bucket("exa") is ratelimit.bucket("exa")


@pytest.mark.parametrize(
    ("header", "seconds"),
    [("12", 12.0), ("0.5", 0.5), (None, ratelimit.DEFAULT_BACKOFF_SECONDS),
     ("Wed, 21 Oct 2026 07:28:00 GMT", ratelimit.DEFAULT_BACKOFF_SECONDS), ("-3", 0.0)],
)
def test_retry_after_parsing(header, seconds):
    assert ratelimit.retry_after_seconds(header) == seconds


async def test_exa_backs_off_on_429(monkeypatch, ledger):
    from datetime import datetime, timezone

    import httpx

    from app.services.news.base import NewsSearchRequest
    from app.services.news.exa import ExaNewsProvider

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "4"}, text="slow down")

    client = httpx.AsyncClient(base_url="https://exa.test", transport=httpx.MockTransport(handler))
    backoffs: list[float] = []
    monkeypatch.setattr(ratelimit.ProviderLimiter, "backoff", lambda self, s: backoffs.append(s))
    # No real waiting between tenacity's retries.
    monkeypatch.setattr("app.services.news.exa.ExaNewsProvider._post.retry.sleep", _no_sleep)

    provider = ExaNewsProvider(api_key="k", client=client)
    with pytest.raises(Exception):
        await provider.execute(
            NewsSearchRequest(
                query="q",
                start=datetime(2026, 7, 1, tzinfo=timezone.utc),
                end=datetime(2026, 7, 2, tzinfo=timezone.utc),
            )
        )

    assert backoffs and all(s == 4.0 for s in backoffs)
    assert len(calls) == len(backoffs)


async def _no_sleep(_seconds: float) -> None:
    return None


# ============================================ shared, account-wide limits

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL")
REDIS = [
    pytest.mark.redis,
    pytest.mark.skipif(not TEST_REDIS_URL, reason="TEST_REDIS_URL is not set"),
]


class VirtualTime:
    """A clock and a sleep for many concurrent tasks: `run` lets every task
    go as far as it can, then jumps time to the earliest sleeper."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.now + max(seconds, 0.0), next(self._seq), future))
        await future

    async def run(self, until: float) -> None:
        while True:
            for _ in range(50):  # let every runnable task reach its next sleep
                await asyncio.sleep(0)
            if not self._sleepers or self._sleepers[0][0] > until:
                self.now = until
                return
            at, _, future = heapq.heappop(self._sleepers)
            self.now = max(self.now, at)
            future.set_result(None)


@pytest.fixture
def account(monkeypatch):
    """Exa at 10 calls a second for the whole account, half of it for background."""
    monkeypatch.setattr(settings, "exa_max_rps", 10.0)
    monkeypatch.setattr(settings, "background_rate_share", 0.5)
    monkeypatch.setattr(settings, "rate_limit_max_wait_seconds", 60.0)
    monkeypatch.setattr(settings, "rate_limit_interactive_max_wait_seconds", 5.0)
    monkeypatch.setattr(settings, "expected_processes", 2)


def limiter_on(store, clock, sleep) -> ProviderLimiter:
    return ProviderLimiter("exa", store, clock=clock, sleep=sleep)


# ------------------------------------------------------------ the schedule


def test_the_schedule_allows_a_burst_then_spaces_calls():
    tat, waits = None, []
    for _ in range(7):
        tat, wait = schedule(tat, 100.0, 0.0, rate=5.0)
        waits.append(round(wait, 6))
    assert waits == [0, 0, 0, 0, 0, 0.2, 0.4]


def test_a_block_spreads_reservations_out_after_it():
    tat, waits = None, []
    for _ in range(3):
        tat, wait = schedule(tat, 100.0, 103.0, rate=1.0)
        waits.append(round(wait, 6))
    assert waits == [3.0, 4.0, 5.0], "not all released at once when the block ends"


# ------------------------------------------------------- the user's reserve


async def test_background_is_held_to_its_share_and_users_still_get_through(account):
    time_ = VirtualTime()
    store = MemoryRateStore(clock=time_)
    worker = limiter_on(store, time_, time_.sleep)  # two "processes" on one store
    api = limiter_on(store, time_, time_.sleep)
    done: dict[str, list[float]] = {"background": [], "interactive": []}

    async def call(limiter, call_class: CallClass, start: float) -> None:
        await time_.sleep(start - time_.now)
        with attributed(call_class=call_class):
            await limiter.acquire()
        done[call_class.value].append(time_.now)

    tasks = [asyncio.create_task(call(worker, CallClass.BACKGROUND, 1_000.0)) for _ in range(60)]
    tasks += [
        asyncio.create_task(call(api, CallClass.INTERACTIVE, 1_000.0 + t)) for t in (2.0, 4.0, 6.0)
    ]
    await time_.run(until=1_008.0)
    for task in tasks:
        task.cancel()

    background = sorted(done["background"])
    # At most a second's burst, then 5 a second: the background share of 10.
    for i, at in enumerate(background):
        assert at - 1_000.0 >= (i - 5) / 5.0 - 1e-6
    assert 40 <= len(background) <= 46
    # Each user call, arriving into a saturated background queue, is served
    # within one provider interval, not behind the backlog.
    arrivals = [2.0, 4.0, 6.0]
    assert [round(at - 1_000.0 - a, 3) <= 0.1 for at, a in zip(sorted(done["interactive"]), arrivals)] == [True] * 3


async def test_without_background_traffic_users_get_the_whole_rate(account):
    clock = FakeClock()
    limiter = limiter_on(MemoryRateStore(clock=clock), clock, clock.sleep)
    with attributed(call_class=CallClass.INTERACTIVE):
        for _ in range(30):
            await limiter.acquire()
    assert clock.now - 1000.0 == pytest.approx((30 - 10) / 10.0)


# ---------------------------------------------------------- waiting, or not


async def test_a_user_waits_at_most_the_interactive_limit_then_gets_503_material(account):
    clock = FakeClock()
    limiter = limiter_on(MemoryRateStore(clock=clock), clock, clock.sleep)
    limiter.backoff(30.0)

    with attributed(call_class=CallClass.INTERACTIVE), pytest.raises(ProviderBusy) as busy:
        await limiter.acquire()
    assert busy.value.provider == "exa"
    assert (busy.value.retry_at - datetime.now(timezone.utc)).total_seconds() == pytest.approx(30, abs=2)
    assert clock.slept == [], "refused at once, not after waiting"

    with attributed(call_class=CallClass.BACKGROUND):
        await limiter.acquire()  # background may wait up to 60 s
    assert sum(clock.slept) == pytest.approx(30.0)


async def test_background_past_its_max_wait_raises_for_the_queue_to_retry(account, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_max_wait_seconds", 10.0)
    clock = FakeClock()
    limiter = limiter_on(MemoryRateStore(clock=clock), clock, clock.sleep)
    limiter.backoff(30.0)
    with attributed(call_class=CallClass.BACKGROUND), pytest.raises(ProviderBusy):
        await limiter.acquire()


# ------------------------------------------------------- sharing a backoff


async def test_a_429_in_one_process_blocks_the_other(account):
    clock = FakeClock()
    store = MemoryRateStore(clock=clock)
    first, second = limiter_on(store, clock, clock.sleep), limiter_on(store, clock, clock.sleep)

    first.backoff(3.0)
    await first.settle()  # the shared write has landed
    with attributed(call_class=CallClass.INTERACTIVE):
        await second.acquire()

    assert clock.slept == [pytest.approx(3.0)]


async def test_a_shorter_backoff_never_cuts_a_longer_one_short(account):
    clock = FakeClock()
    store = MemoryRateStore(clock=clock)
    first, second = limiter_on(store, clock, clock.sleep), limiter_on(store, clock, clock.sleep)
    first.backoff(4.0)
    second.backoff(1.0)
    await first.settle()
    await second.settle()
    third = limiter_on(store, clock, clock.sleep)
    with attributed(call_class=CallClass.INTERACTIVE):
        await third.acquire()
    assert clock.slept == [pytest.approx(4.0)]


# ------------------------------------------------------------ the fallback


class DownStore:
    def __init__(self) -> None:
        self.calls = 0

    async def reserve(self, *args, **kwargs):
        self.calls += 1
        raise RedisConnectionError("Connection refused")

    async def block(self, *args, **kwargs):
        raise RedisConnectionError("Connection refused")


async def test_without_redis_each_process_takes_its_part_of_the_rate(account, monkeypatch):
    events = []
    monkeypatch.setattr(limits.logger, "warning", lambda event, **fields: events.append(event))
    clock = FakeClock()
    down = DownStore()
    limiter = limiter_on(down, clock, clock.sleep)

    with attributed(call_class=CallClass.INTERACTIVE):
        for _ in range(15):
            await limiter.acquire()

    # 10/s across 2 processes: 5/s here, a burst of 5, then 0.2 s apart.
    assert sum(clock.slept) == pytest.approx((15 - 5) / 5.0)
    assert down.calls == 1, "Redis is not retried on every call while it is down"
    assert limiter.degraded and events == ["limiter_fallback"]


async def test_the_fallback_still_holds_background_to_its_share(account):
    clock = FakeClock()
    limiter = limiter_on(DownStore(), clock, clock.sleep)
    with attributed(call_class=CallClass.BACKGROUND):
        for _ in range(12):
            await limiter.acquire()
    # Background share of this process's 5/s is 2.5/s: a burst of 2.5 (2 whole calls), then 0.4 s apart.
    assert sum(clock.slept) == pytest.approx(4.0, abs=0.41)


def test_with_redis_unset_limits_are_per_process(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", None)
    ratelimit.reset()
    assert ratelimit.bucket("exa").store is None


# --------------------------------------------------------- on real Redis


@pytest.fixture
async def redis_store():
    client = aioredis.from_url(TEST_REDIS_URL)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.mark.parametrize("_", [pytest.param(None, marks=REDIS)])
async def test_redis_store_schedules_and_shares_blocks(_, redis_store):
    one, two = RedisRateStore(redis_store), RedisRateStore(redis_store)

    waits = [(await one.reserve("exa", provider="exa", rate=5.0, max_wait=60))[1] for _ in range(7)]
    assert waits[:5] == pytest.approx([0] * 5, abs=0.01)
    assert waits[5] == pytest.approx(0.2, abs=0.05) and waits[6] == pytest.approx(0.4, abs=0.05)

    granted, wait = await two.reserve("exa", provider="exa", rate=5.0, max_wait=0.1)
    assert not granted and wait == pytest.approx(0.6, abs=0.05), "the other process sees the same schedule"

    await one.block("anthropic", 3.0)
    granted, wait = await two.reserve("anthropic", provider="anthropic", rate=1.0, max_wait=10)
    assert granted and wait == pytest.approx(3.0, abs=0.05)
    await one.block("anthropic", 1.0)
    assert (await redis_store.get("metrix:ratelimit:anthropic:blocked_until")) is not None
    granted, wait = await two.reserve("anthropic:background", provider="anthropic", rate=1.0, max_wait=10)
    assert wait == pytest.approx(3.0, abs=0.1), "a shorter block does not shorten a longer one"


# ---------------------------------------- a busy provider, end to end

def busy(provider: str = "anthropic") -> ProviderBusy:
    return ProviderBusy(provider, datetime.now(timezone.utc) + timedelta(seconds=20), f"{provider} busy")


class BusyLLM(StubLLM):
    async def parse_structured(self, *, output_model, **kwargs):
        from app.services.relevance import RelevanceReport

        if output_model is RelevanceReport:
            raise busy()
        return await super().parse_structured(output_model=output_model, **kwargs)

    async def complete(self, **kwargs):
        raise busy()


async def test_a_busy_provider_retries_the_job_and_spares_the_movement(db):
    async with db() as session:
        ticker = await ingestion.get_or_create_ticker(session, "BUSY")
        (movement,) = (await ingestion.refresh_prices(session, ticker, build_price_history("BUSY"))).created
        job = await queue.enqueue(session, JobKind.ENRICH_MOVEMENT, {"movement_id": movement.id},
                                  priority=40, dedupe_key=queue.enrich_key(movement.id),
                                  source=JobSource.SCHEDULED)
        await session.commit()

    await Worker(session_factory=db, llm=BusyLLM()).run_once()

    async with db() as session:
        retried = await session.get(Job, job.id)
        assert (retried.status, retried.attempts) == (JobStatus.QUEUED, 1)
        assert "ProviderBusy" in retried.last_error and retried.hold_reason is None
        assert retried.run_after.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
        stored = await session.get(Movement, movement.id)
        assert (stored.news_status, stored.news_attempts) == (NewsStatus.PENDING, 0)


async def test_a_busy_price_source_releases_the_ticker_unharmed(session_factory, monkeypatch):
    async def busy_fetch(symbol, days=None):
        raise busy("yfinance")

    monkeypatch.setattr(price_service, "fetch_price_history", busy_fetch)
    async with session_factory() as session:
        ticker = await ingestion.get_or_create_ticker(session, "WAITY")
        assert await ingestion.claim_ingestion(session, ticker)
        with pytest.raises(ProviderBusy):
            await ingestion.ingest_ticker(session, "WAITY", llm=StubLLM())
        ticker = await ingestion.get_ticker(session, "WAITY")
        assert ticker.ingest_status == IngestStatus.PENDING and ticker.ingest_error is None
        assert await ingestion.claim_ingestion(session, ticker), "free to claim again"


async def test_chat_with_a_busy_model_is_503_and_keeps_the_quota(session_factory, monkeypatch):
    monkeypatch.setitem(
        settings.plan_limits_json, "free",
        settings.plan_limits_json["free"].model_copy(update={"chat_per_day": 1}),
    )
    _, key = await new_api_key(session_factory, plan=Plan.FREE)
    async with api_client(api_app(session_factory, BusyLLM()), key) as client:
        response = await client.post("/chat", json={"question": "Why?"})
    assert response.status_code == 503
    assert response.json()["error"] == "upstream_unavailable"
    assert 1 <= int(response.headers["retry-after"]) <= 21

    async with api_client(api_app(session_factory, StubLLM()), key) as client:
        assert (await client.post("/chat", json={"question": "Why?"})).status_code == 200


async def test_an_inline_ingestion_with_prices_busy_is_503_and_refunded(session_factory, monkeypatch):
    async def busy_fetch(symbol, days=None):
        raise busy("yfinance")

    monkeypatch.setattr(price_service, "fetch_price_history", busy_fetch)
    monkeypatch.setitem(
        settings.plan_limits_json, "pro",
        settings.plan_limits_json["pro"].model_copy(update={"cold_ingests_per_day": 1}),
    )
    _, key = await new_api_key(session_factory, plan=Plan.PRO)
    async with api_client(api_app(session_factory, StubLLM()), key) as client:
        assert (await client.get("/tickers/WAITY", params={"wait": True})).status_code == 503

        async def working_fetch(symbol, days=None):
            return build_price_history(symbol)

        monkeypatch.setattr(price_service, "fetch_price_history", working_fetch)
        retry = await client.get("/tickers/WAITY", params={"wait": True})
    assert retry.status_code == 200 and retry.json()["status"] == "ready"
