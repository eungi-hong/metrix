"""Tests for the per-provider token buckets, on a fake clock."""

from __future__ import annotations

import pytest

from app.services import ratelimit
from app.services.ratelimit import TokenBucket


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
    import httpx

    from app.services.news.exa import ExaNewsProvider
    from app.services.news.base import NewsSearchRequest
    from datetime import datetime, timezone

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "4"}, text="slow down")

    client = httpx.AsyncClient(base_url="https://exa.test", transport=httpx.MockTransport(handler))
    backoffs: list[float] = []
    monkeypatch.setattr(TokenBucket, "backoff", lambda self, s: backoffs.append(s))
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
