"""Tests for per-user quotas through the API: 429s and their headers, chat's
charge-then-refund, cold ingests and refreshes, wait=true and its inline
slots, and the readiness view of Redis."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.core.config import PlanLimits, settings
from app.core.errors import LLMError, PriceDataError, SpendCapReached
from app.models.enums import IngestStatus
from app.models.identity import Plan
from app.models.jobs import Job, JobStatus
from app.services import ingestion, quotas
from app.services import prices as price_service
from tests.conftest import StubLLM, api_app, api_client, build_price_history, new_api_key

GENEROUS = dict(
    requests_per_minute=1000, chat_per_minute=1000, chat_per_day=1000,
    cold_ingests_per_day=1000, refresh_per_day=1000, allow_wait=True,
)


def plan(monkeypatch, name: str = "free", **limits) -> None:
    monkeypatch.setitem(settings.plan_limits_json, name, PlanLimits(**(GENEROUS | limits)))


@pytest.fixture
def logged(monkeypatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(quotas.logger, "info", lambda event, **f: events.append((event, f)))
    return events


async def free_client(session_factory, llm=None):
    _, key = await new_api_key(session_factory, plan=Plan.FREE)
    return api_client(api_app(session_factory, llm or StubLLM()), key)


async def stored_ticker(session_factory, symbol: str, *, age: timedelta) -> None:
    """A ticker with prices and movements, last ingested `age` ago."""
    async with session_factory() as session:
        ticker = await ingestion.get_or_create_ticker(session, symbol)
        await ingestion.refresh_prices(session, ticker, build_price_history(symbol))
        ticker.ingest_status = IngestStatus.COMPLETE
        ticker.last_ingested_at = datetime.now(timezone.utc) - age
        await session.commit()


async def finish_jobs(session_factory) -> None:
    async with session_factory() as session:
        await session.execute(sa.update(Job).values(status=JobStatus.SUCCEEDED))
        await session.commit()


# ------------------------------------------------------- per request


async def test_every_call_counts_and_the_limit_answers_429_with_headers(session_factory, monkeypatch, logged):
    plan(monkeypatch, requests_per_minute=3)
    async with await free_client(session_factory) as client:
        ok = [await client.get("/conversations") for _ in range(3)]
        refused = await client.get("/conversations")

    assert [r.status_code for r in ok] == [200, 200, 200]
    assert [r.headers["x-ratelimit-remaining"] for r in ok] == ["2", "1", "0"]
    assert all(r.headers["x-ratelimit-limit"] == "3" for r in ok)
    assert 0 < int(ok[0].headers["x-ratelimit-reset"]) <= 60

    assert refused.status_code == 429
    assert refused.json()["error"] == "rate_limited"
    assert "requests_per_minute" in refused.json()["detail"]
    assert refused.headers["retry-after"] == "20"  # one more cell: 60 s / 3
    assert refused.headers["x-ratelimit-remaining"] == "0"
    assert logged[-1] == ("quota_exceeded", logged[-1][1])
    assert logged[-1][1]["quota"] == "requests_per_minute"


async def test_callers_have_separate_allowances(session_factory, monkeypatch):
    plan(monkeypatch, requests_per_minute=1)
    async with await free_client(session_factory) as alice, await free_client(session_factory) as bob:
        assert (await alice.get("/conversations")).status_code == 200
        assert (await alice.get("/conversations")).status_code == 429
        assert (await bob.get("/conversations")).status_code == 200


async def test_anonymous_callers_get_the_anonymous_plan(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    plan(monkeypatch, "anonymous", requests_per_minute=1)
    async with api_client(api_app(session_factory, StubLLM())) as client:
        assert (await client.get("/conversations")).status_code == 200
        assert (await client.get("/conversations")).status_code == 429


# --------------------------------------------------------------- chat


class FailingLLM(StubLLM):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def complete(self, **kwargs):
        raise self.error


async def test_chat_has_its_own_daily_quota(session_factory, monkeypatch):
    plan(monkeypatch, chat_per_day=2)
    async with await free_client(session_factory) as client:
        answers = [await client.post("/chat", json={"question": f"Q{i}?"}) for i in range(3)]
        assert (await client.get("/conversations")).status_code == 200, "other calls unaffected"

    assert [a.status_code for a in answers] == [200, 200, 429]
    assert "chat_per_day" in answers[2].json()["detail"]
    reset = int(answers[2].headers["retry-after"])
    tomorrow = datetime.combine(datetime.now(timezone.utc).date() + timedelta(days=1), datetime.min.time(), timezone.utc)
    assert abs(reset - (tomorrow - datetime.now(timezone.utc)).total_seconds()) < 5


async def test_chat_per_minute_refuses_bursts(session_factory, monkeypatch):
    plan(monkeypatch, chat_per_minute=1)
    async with await free_client(session_factory) as client:
        assert (await client.post("/chat", json={"question": "Q?"})).status_code == 200
        refused = await client.post("/chat", json={"question": "Q?"})
    assert refused.status_code == 429 and "chat_per_minute" in refused.json()["detail"]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (LLMError("anthropic", "overloaded"), 502),
        (SpendCapReached("interactive", datetime.now(timezone.utc) + timedelta(hours=1), "cap"), 503),
    ],
)
async def test_a_turn_that_fails_on_our_side_gives_its_quota_back(session_factory, monkeypatch, error, status):
    plan(monkeypatch, chat_per_day=1, chat_per_minute=1)
    _, key = await new_api_key(session_factory, plan=Plan.FREE)
    async with api_client(api_app(session_factory, FailingLLM(error)), key) as failing:
        assert (await failing.post("/chat", json={"question": "Q?"})).status_code == status
    async with api_client(api_app(session_factory, StubLLM()), key) as working:
        assert (await working.post("/chat", json={"question": "Q?"})).status_code == 200


async def test_a_turn_the_caller_got_wrong_keeps_its_charge(session_factory, monkeypatch):
    plan(monkeypatch, chat_per_day=1)
    async with await free_client(session_factory) as client:
        probe = await client.post("/chat", json={"question": "Q?", "conversation_id": "someone-elses"})
        assert probe.status_code == 404
        assert (await client.post("/chat", json={"question": "Q?"})).status_code == 429


# ------------------------------------------------------- cold ingests


async def test_a_cold_ticker_is_charged_once_and_joining_it_is_free(session_factory, monkeypatch):
    plan(monkeypatch, cold_ingests_per_day=1)
    async with await free_client(session_factory) as alice, await free_client(session_factory) as bob:
        first = await alice.get("/tickers/COLD")
        again = await alice.get("/tickers/COLD")  # joins the queued job
        joined = await bob.get("/tickers/COLD")
        bob_cold = await bob.get("/tickers/OTHER")  # bob's own allowance is untouched
        joins_bob = await alice.get("/tickers/OTHER")  # queued by bob: free for alice too
        second = await alice.get("/tickers/THIRD")

    assert first.status_code == 202 and again.json()["job_id"] == first.json()["job_id"]
    assert joined.json()["job_id"] == first.json()["job_id"]
    assert bob_cold.status_code == 202
    assert joins_bob.json()["job_id"] == bob_cold.json()["job_id"]
    assert second.status_code == 429 and "cold_ingests_per_day" in second.json()["detail"]


async def test_a_warm_ticker_never_counts(session_factory, monkeypatch):
    plan(monkeypatch, cold_ingests_per_day=0)
    await stored_ticker(session_factory, "WARM", age=timedelta(minutes=5))
    async with session_factory() as session:  # nothing owed: every movement has its news
        await session.execute(sa.text("UPDATE movements SET news_status = 'complete'"))
        await session.commit()
    async with await free_client(session_factory) as client:
        response = await client.get("/tickers/WARM")
    assert response.status_code == 200 and response.json()["status"] == "ready"


async def test_over_quota_a_stale_ticker_is_served_from_storage(session_factory, monkeypatch):
    plan(monkeypatch, cold_ingests_per_day=0)
    await stored_ticker(session_factory, "STALE", age=timedelta(days=3))
    async with await free_client(session_factory) as client:
        response = await client.get("/tickers/STALE")

    body = response.json()
    assert response.status_code == 200 and body["movements"]
    assert body["job_id"] is None
    assert any("cold_ingests_per_day" in w for w in body["warnings"])
    async with session_factory() as session:
        assert (await session.scalar(sa.select(sa.func.count()).select_from(Job))) == 0


async def test_owed_news_is_charged_once_per_request(session_factory, monkeypatch):
    plan(monkeypatch, cold_ingests_per_day=1)
    await stored_ticker(session_factory, "OWED", age=timedelta(minutes=5))
    async with await free_client(session_factory) as client:
        first = await client.get("/tickers/OWED")
        again = await client.get("/tickers/OWED")  # its jobs are queued: free
        await finish_jobs(session_factory)
        third = await client.get("/tickers/OWED")  # new work, no quota left

    assert first.json()["status"] == "refreshing" and first.json()["job_id"]
    assert again.json()["job_id"] == first.json()["job_id"]
    assert third.status_code == 200 and third.json()["job_id"] is None
    assert any("cold_ingests_per_day" in w for w in third.json()["warnings"])


# ------------------------------------------------------------ refresh


async def test_refresh_has_its_own_daily_quota(session_factory, monkeypatch):
    plan(monkeypatch, refresh_per_day=1, cold_ingests_per_day=0)
    await stored_ticker(session_factory, "FRESH", age=timedelta(minutes=5))
    async with await free_client(session_factory) as client:
        first = await client.get("/tickers/FRESH", params={"refresh": True})
        await finish_jobs(session_factory)
        second = await client.get("/tickers/FRESH", params={"refresh": True})

    assert first.status_code == 200 and first.json()["status"] == "refreshing"
    assert second.status_code == 429 and "refresh_per_day" in second.json()["detail"]


# --------------------------------------------------------------- wait


async def test_wait_on_a_plan_without_it_is_queued_instead(session_factory, monkeypatch):
    plan(monkeypatch, allow_wait=False)
    async with await free_client(session_factory) as client:
        response = await client.get("/tickers/COLD", params={"wait": True})

    assert response.status_code == 202 and response.json()["job_id"]
    assert any("not available on the free plan" in w for w in response.json()["warnings"])


async def test_wait_runs_inline_where_the_plan_allows_it(session_factory, monkeypatch):
    plan(monkeypatch, cold_ingests_per_day=1)
    async with await free_client(session_factory) as client:
        response = await client.get("/tickers/INLINE", params={"wait": True})
        assert response.status_code == 200 and response.json()["status"] == "ready"
        assert (await client.get("/tickers/INLINE2", params={"wait": True})).status_code == 429


async def test_inline_work_is_bounded_per_process(session_factory, monkeypatch):
    """With the only slot taken (as by a long inline ingestion), wait=true is
    429 at once, while the same request without it is queued as usual."""
    from app.api.routes import tickers

    monkeypatch.setattr(settings, "api_max_inline_ingestions", 1)
    plan(monkeypatch)
    async with await free_client(session_factory) as client:
        async with tickers._inline_slot():
            busy = await client.get("/tickers/BUSY", params={"wait": True})
            queued = await client.get("/tickers/BUSY")
        after = await client.get("/tickers/AFTER", params={"wait": True})

    assert after.status_code == 200, "the slot is free again"
    assert busy.status_code == 429
    assert busy.json()["error"] == "rate_limited" and busy.headers["retry-after"] == "5"
    assert queued.status_code == 202


async def test_an_inline_ingestion_that_fails_upstream_is_refunded(session_factory, monkeypatch):
    plan(monkeypatch, cold_ingests_per_day=1)

    async def down(symbol: str, days: int | None = None):
        raise PriceDataError("yfinance", "timed out")

    monkeypatch.setattr(price_service, "fetch_price_history", down)
    async with await free_client(session_factory) as client:
        assert (await client.get("/tickers/DOWN", params={"wait": True})).status_code == 502
        monkeypatch.undo()
        plan(monkeypatch, cold_ingests_per_day=1)
        assert (await client.get("/tickers/UP")).status_code == 202, "the quota came back"


# ------------------------------------------------------------- health


async def test_health_reports_redis(client, monkeypatch):
    assert (await client.get("/health")).json()["redis"] == "not_configured"

    async def unreachable() -> str:
        return "unreachable"

    from app.core import redis as redis_module

    monkeypatch.setattr(redis_module, "ping", unreachable)
    body = (await client.get("/health")).json()
    assert body["redis"] == "unreachable" and body["status"] == "degraded"
