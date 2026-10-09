"""Tests for the spend ledger and the daily cap: prices, reserve and settle,
the background share, the seams that meter, how a refusal travels through the
pipeline, the worker and the API, and the admin usage view."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
import sqlalchemy as sa
from pydantic import BaseModel

from app.core import context
from app.core.config import LLMPrice, Settings, settings
from app.core.context import CallClass, attributed
from app.core.errors import SpendCapReached
from app.models.enums import IngestStatus, NewsStatus
from app.models.jobs import Job, JobKind, JobSource, JobStatus, PrewarmRun
from app.models.market import Movement
from app.models.usage import SpendDaily, UsageEvent
from app.services import ingestion, prewarm, queue, spend
from app.services.llm.anthropic import AnthropicProvider
from app.services.news.base import NewsProvider, NewsSearchRequest
from app.services.news.cache import CachingNewsProvider
from app.services.news.exa import ExaNewsProvider
from app.services.relevance import RelevanceReport
from app.worker import Worker, call_class_for
from tests.conftest import StubLLM, api_app, api_client, build_price_history

PRICES = {
    "test-model": LLMPrice(
        input_per_mtok=3.0, output_per_mtok=15.0, cache_read_per_mtok=0.3, cache_write_per_mtok=3.75
    )
}


class RecordingLogger:
    """Stands in for a module's structlog logger; keeps (level, event, fields)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str):
        def log(event: str, **fields: Any) -> None:
            self.events.append((level, event, fields))

        return log

    def __getattr__(self, level: str):
        return self._record(level)

    def named(self, event: str) -> list[dict[str, Any]]:
        return [fields for _, name, fields in self.events if name == event]


@pytest.fixture
def logs(monkeypatch) -> RecordingLogger:
    recorder = RecordingLogger()
    monkeypatch.setattr(spend, "logger", recorder)
    return recorder


@pytest.fixture
def priced(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_prices_json", PRICES)


def cap(monkeypatch, dollars: float | None, share: float = 0.6) -> None:
    monkeypatch.setattr(settings, "daily_spend_cap_usd", dollars)
    monkeypatch.setattr(settings, "background_spend_share", share)


async def ledger_rows(factory) -> list[UsageEvent]:
    async with factory() as session:
        return list((await session.scalars(sa.select(UsageEvent).order_by(UsageEvent.id))).all())


async def today_row(factory) -> SpendDaily | None:
    async with factory() as session:
        return await session.get(SpendDaily, spend.utc_day(datetime.now(timezone.utc)))


async def charge(dollars: str, *, call_class: CallClass, operation: str = "search") -> None:
    with attributed(call_class=call_class):
        async with spend.metered("exa", operation, Decimal(dollars)) as meter:
            await meter.settle(cost_usd=Decimal(dollars), estimated=False)


# ------------------------------------------------------------------ prices


def test_llm_cost_uses_the_configured_prices(priced):
    cost, estimated = spend.llm_cost(
        "test-model",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=200_000,
        cache_write_tokens=0,
    )
    assert (cost, estimated) == (Decimal("4.560000"), False)  # 3 + 1.5 + 0.06


def test_an_unpriced_model_is_charged_the_fallback_and_flagged(monkeypatch):
    monkeypatch.setattr(settings, "llm_fallback_input_per_mtok", 10.0)
    monkeypatch.setattr(settings, "llm_fallback_output_per_mtok", 50.0)

    cost, estimated = spend.llm_cost("mystery-model", input_tokens=1_000_000, output_tokens=0)

    assert (cost, estimated) == (Decimal("10.000000"), True)


def test_the_estimate_is_never_below_a_realistic_call(priced):
    prompt = "x" * 40_000  # ~10k real tokens at 4 chars each
    estimate = spend.llm_estimate("test-model", prompt_chars=len(prompt), max_tokens=1024)
    actual, _ = spend.llm_cost("test-model", input_tokens=10_000, output_tokens=1024)
    assert estimate > actual


def test_exa_cost_comes_from_the_response_or_the_estimate(monkeypatch):
    monkeypatch.setattr(settings, "exa_cost_estimate_usd", 0.02)
    assert spend.exa_cost({"costDollars": {"total": 0.007}}) == (Decimal("0.007000"), False)
    assert spend.exa_cost({}) == (Decimal("0.020000"), True)
    assert spend.exa_cost({"costDollars": {"total": None}}) == (Decimal("0.020000"), True)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("{not json", "not valid JSON"),
        ('[{"input_per_mtok": 1}]', "JSON object keyed by model id"),
        ('{"m": {"input_per_mtok": 1}}', "entry for 'm'"),
        ('{"m": {"input_per_mtok": -1, "output_per_mtok": 1, '
         '"cache_read_per_mtok": 1, "cache_write_per_mtok": 1}}', "entry for 'm'"),
    ],
)
def test_bad_price_json_fails_at_startup_with_a_clear_message(value, message):
    with pytest.raises(ValueError, match=message):
        Settings(llm_prices_json=value)


def test_prod_requires_a_cap_and_shares_must_be_fractions():
    with pytest.raises(ValueError, match="DAILY_SPEND_CAP_USD is required"):
        Settings(app_env="prod")
    with pytest.raises(ValueError):
        Settings(background_spend_share=1.5)
    assert Settings(app_env="prod", daily_spend_cap_usd=50).daily_spend_cap_usd == 50


def test_an_unpriced_model_is_announced_at_startup(logs, monkeypatch):
    monkeypatch.setattr(settings, "llm_model", "mystery-model")
    spend.warn_on_startup()
    assert logs.named("llm_model_unpriced")[0]["model"] == "mystery-model"
    assert logs.named("spend_cap_unset")


# ------------------------------------------------------- reserve and settle


async def test_settling_records_the_call_with_its_attribution(ledger):
    with attributed(call_class=CallClass.BACKGROUND, job_id=None, user_id=7):
        async with spend.metered("anthropic", "relevance", Decimal("0.05")) as meter:
            await meter.settle(
                cost_usd=Decimal("0.0123"), estimated=False, model="test-model",
                input_tokens=1000, output_tokens=200,
            )

    (row,) = await ledger_rows(ledger)
    assert (row.provider, row.operation, row.model) == ("anthropic", "relevance", "test-model")
    assert (row.user_id, row.job_id, row.call_class) == (7, None, CallClass.BACKGROUND)
    assert (row.input_tokens, row.output_tokens, row.cost_estimated) == (1000, 200, False)
    day = await today_row(ledger)
    assert day.spent_usd == Decimal("0.0123") and day.background_spent_usd == Decimal("0.0123")
    assert day.reserved_usd == 0 and day.background_reserved_usd == 0


async def test_a_failed_call_records_nothing_and_releases_its_reservation(ledger):
    with pytest.raises(RuntimeError):
        async with spend.metered("exa", "search", Decimal("0.05")):
            raise RuntimeError("upstream down")

    assert await ledger_rows(ledger) == []
    day = await today_row(ledger)
    assert day.spent_usd == 0 and day.reserved_usd == 0


async def test_a_ledger_write_failure_never_fails_the_call(ledger, logs, monkeypatch):
    reservation = await spend.reserve("exa", "search", Decimal("0.01"))
    monkeypatch.setattr(spend, "_session_factory", lambda: (_ for _ in ()).throw(OSError("db gone")))

    await reservation.settle(cost_usd=Decimal("0.01"), estimated=False)  # does not raise

    assert logs.named("spend_record_failed")


async def test_reserving_fails_closed_when_the_database_is_unreachable(monkeypatch):
    def unreachable():
        raise OSError("connection refused")

    monkeypatch.setattr(spend, "_session_factory", unreachable)
    with pytest.raises(OSError):
        await spend.reserve("exa", "search", Decimal("0.01"))


# ------------------------------------------------------------------- caps


async def test_interactive_calls_may_use_the_whole_cap_and_no_more(ledger, logs, monkeypatch):
    cap(monkeypatch, 1.00)
    await charge("0.95", call_class=CallClass.INTERACTIVE)

    with attributed(call_class=CallClass.INTERACTIVE):
        await spend.reserve("exa", "search", Decimal("0.05"))  # exactly fits
        with pytest.raises(SpendCapReached) as refused:
            await spend.reserve("exa", "search", Decimal("0.01"))

    assert refused.value.call_class == "interactive"
    assert refused.value.retry_at == spend.next_reset(datetime.now(timezone.utc))
    assert logs.named("spend_cap_reached")[0]["call_class"] == "interactive"


async def test_background_stops_at_its_share_while_users_still_get_through(ledger, monkeypatch):
    cap(monkeypatch, 1.00, share=0.6)
    await charge("0.60", call_class=CallClass.BACKGROUND)

    with attributed(call_class=CallClass.BACKGROUND):
        with pytest.raises(SpendCapReached) as refused:
            await spend.reserve("anthropic", "relevance", Decimal("0.01"))
    assert "background share" in str(refused.value)

    await charge("0.30", call_class=CallClass.INTERACTIVE)  # the reserve is intact


async def test_background_is_also_bound_by_the_whole_cap(ledger, monkeypatch):
    cap(monkeypatch, 1.00, share=0.6)
    await charge("0.95", call_class=CallClass.INTERACTIVE)

    with attributed(call_class=CallClass.BACKGROUND), pytest.raises(SpendCapReached):
        await spend.reserve("exa", "search", Decimal("0.10"))


async def test_concurrent_reservations_cannot_jointly_overspend(ledger, monkeypatch):
    cap(monkeypatch, 1.00)

    async def attempt() -> bool:
        with attributed(call_class=CallClass.INTERACTIVE):
            try:
                await spend.reserve("exa", "search", Decimal("0.30"))
            except SpendCapReached:
                return False
        return True

    granted = await asyncio.gather(*(attempt() for _ in range(8)))

    assert sum(granted) == 3  # 0.90 fits under 1.00; a fourth would not
    assert (await today_row(ledger)).reserved_usd == Decimal("0.90")


async def test_the_refusal_is_logged_once_a_day_per_class(ledger, logs, monkeypatch):
    cap(monkeypatch, 0.10)
    for _ in range(3):
        with attributed(call_class=CallClass.INTERACTIVE), pytest.raises(SpendCapReached):
            await spend.reserve("exa", "search", Decimal("0.50"))
    with attributed(call_class=CallClass.BACKGROUND), pytest.raises(SpendCapReached):
        await spend.reserve("exa", "search", Decimal("0.50"))

    reached = logs.named("spend_cap_reached")
    assert [r["call_class"] for r in reached] == ["interactive", "background"]


async def test_the_alert_fires_once_when_spend_crosses_its_fraction(ledger, logs, monkeypatch):
    cap(monkeypatch, 1.00)
    monkeypatch.setattr(settings, "spend_alert_fraction", 0.8)

    await charge("0.70", call_class=CallClass.INTERACTIVE)
    assert logs.named("spend_threshold_crossed") == []
    await charge("0.15", call_class=CallClass.INTERACTIVE)
    await charge("0.05", call_class=CallClass.INTERACTIVE)

    assert len(logs.named("spend_threshold_crossed")) == 1


async def test_without_a_cap_spend_is_recorded_but_never_refused(ledger):
    await charge("1000", call_class=CallClass.BACKGROUND)
    assert (await today_row(ledger)).spent_usd == Decimal("1000")


def test_the_reset_is_the_next_utc_midnight():
    late = datetime(2026, 10, 9, 23, 59, 59, tzinfo=timezone.utc)
    assert spend.next_reset(late) == datetime(2026, 10, 10, tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo

    new_york_evening = datetime(2026, 10, 9, 21, 0, tzinfo=ZoneInfo("America/New_York"))
    assert spend.next_reset(new_york_evening) == datetime(2026, 10, 11, tzinfo=timezone.utc)


# ------------------------------------------------------------------ seams


class Scored(BaseModel):
    verdict: str


def fake_anthropic(*, usage: Any = None, parsed: Any = None, error: Exception | None = None):
    """An object shaped like `anthropic.AsyncAnthropic` as far as the seam uses it."""

    async def call(**kwargs: Any) -> Any:
        if error is not None:
            raise error
        return SimpleNamespace(
            model=kwargs["model"],
            usage=usage,
            parsed_output=parsed,
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="an answer")],
        )

    return SimpleNamespace(messages=SimpleNamespace(parse=call, create=call))


def usage(inp: int, out: int, cache_read: int = 0, cache_write: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


async def test_the_anthropic_seam_records_each_call(ledger, priced):
    llm = AnthropicProvider(
        model="test-model", client=fake_anthropic(usage=usage(2000, 300, 1000), parsed=Scored(verdict="ok"))
    )

    with attributed(call_class=CallClass.INTERACTIVE, user_id=3):
        await llm.parse_structured(system="s", user="u", output_model=Scored, operation="relevance")
        await llm.complete(system="s", messages=[{"role": "user", "content": "q"}], operation="chat")

    rows = await ledger_rows(ledger)
    assert [(r.operation, r.call_class, r.user_id) for r in rows] == [
        ("relevance", CallClass.INTERACTIVE, 3),
        ("chat", CallClass.INTERACTIVE, 3),
    ]
    assert (rows[0].input_tokens, rows[0].output_tokens, rows[0].cache_read_tokens) == (2000, 300, 1000)
    assert rows[0].cost_usd == Decimal("0.010800")  # 0.006 + 0.0045 + 0.0003
    assert not rows[0].cost_estimated


async def test_an_unpriced_model_is_recorded_as_estimated(ledger):
    llm = AnthropicProvider(model="mystery-model", client=fake_anthropic(usage=usage(10, 10), parsed=Scored(verdict="ok")))
    await llm.parse_structured(system="s", user="u", output_model=Scored, operation="peers")
    (row,) = await ledger_rows(ledger)
    assert row.cost_estimated and row.model == "mystery-model"


async def test_a_billed_but_unusable_response_is_still_recorded(ledger, priced):
    llm = AnthropicProvider(model="test-model", client=fake_anthropic(usage=usage(100, 50), parsed=None))
    with pytest.raises(Exception, match="unparseable"):
        await llm.parse_structured(system="s", user="u", output_model=Scored, operation="relevance")
    assert len(await ledger_rows(ledger)) == 1


async def test_a_failed_llm_call_records_nothing(ledger, priced):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.InternalServerError(
        "boom", response=httpx.Response(500, request=request), body=None
    )
    llm = AnthropicProvider(model="test-model", client=fake_anthropic(error=error))
    with pytest.raises(Exception):
        await llm.complete(system="s", messages=[{"role": "user", "content": "q"}], operation="chat")
    assert await ledger_rows(ledger) == []
    assert (await today_row(ledger)).reserved_usd == 0


async def test_a_refused_llm_call_is_never_sent(ledger, priced, monkeypatch):
    cap(monkeypatch, 0.000001)
    sent = []

    async def call(**kwargs: Any) -> Any:
        sent.append(kwargs)

    client = SimpleNamespace(messages=SimpleNamespace(parse=call, create=call))
    llm = AnthropicProvider(model="test-model", client=client)
    with attributed(call_class=CallClass.INTERACTIVE), pytest.raises(SpendCapReached):
        await llm.complete(system="s", messages=[{"role": "user", "content": "q"}], operation="chat")
    assert sent == []


def exa_client(payload: dict[str, Any], calls: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(base_url="https://exa.test", transport=httpx.MockTransport(handler))


def search_request() -> NewsSearchRequest:
    return NewsSearchRequest(
        query="q",
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )


async def test_the_exa_seam_records_exas_own_cost(ledger):
    calls: list[httpx.Request] = []
    provider = ExaNewsProvider(api_key="k", client=exa_client({"results": [], "costDollars": {"total": 0.008}}, calls))

    await provider.execute(search_request())

    (row,) = await ledger_rows(ledger)
    assert (row.provider, row.operation, row.cost_usd, row.cost_estimated) == (
        "exa", "search", Decimal("0.008000"), False,
    )


async def test_an_exa_response_without_a_cost_is_recorded_as_estimated(ledger, monkeypatch):
    monkeypatch.setattr(settings, "exa_cost_estimate_usd", 0.04)
    provider = ExaNewsProvider(api_key="k", client=exa_client({"results": []}, []))
    await provider.execute(search_request())
    (row,) = await ledger_rows(ledger)
    assert (row.cost_usd, row.cost_estimated) == (Decimal("0.040000"), True)


async def test_a_refused_search_is_a_refusal_not_a_failed_search(ledger, monkeypatch):
    cap(monkeypatch, 0.01)
    calls: list[httpx.Request] = []
    provider = ExaNewsProvider(api_key="k", client=exa_client({"results": []}, calls))
    with attributed(call_class=CallClass.INTERACTIVE), pytest.raises(SpendCapReached):
        await provider.execute(search_request())
    assert calls == []


async def test_a_cache_hit_costs_and_records_nothing(ledger, session):
    calls: list[httpx.Request] = []
    exa = ExaNewsProvider(api_key="k", client=exa_client({"results": [], "costDollars": {"total": 0.01}}, calls))
    cached = CachingNewsProvider(exa, session)

    await cached.search(search_request())
    await cached.search(search_request())

    assert len(calls) == 1
    assert len(await ledger_rows(ledger)) == 1


# --------------------------------------------- through the pipeline and the queue


def refusal() -> SpendCapReached:
    return SpendCapReached(
        "background", spend.next_reset(datetime.now(timezone.utc)), "cap reached for the test"
    )


class CappedLLM(StubLLM):
    """Resolves peers, then is refused by the cap when asked to score."""

    async def parse_structured(self, *, output_model, **kwargs: Any) -> Any:
        if output_model is RelevanceReport:
            raise refusal()
        return await super().parse_structured(output_model=output_model, **kwargs)


class CappedNews(NewsProvider):
    name = "capped"

    async def execute(self, request: NewsSearchRequest) -> dict[str, Any]:
        raise refusal()

    def parse(self, raw: dict[str, Any]) -> list:
        return []


async def pending_movement(session_factory, symbol: str = "CAP") -> Movement:
    async with session_factory() as session:
        ticker = await ingestion.get_or_create_ticker(session, symbol)
        (movement,) = (
            await ingestion.refresh_prices(session, ticker, build_price_history(symbol))
        ).created
        await session.commit()
        return movement


async def test_a_refused_enrichment_leaves_the_movement_as_it_was(db):
    movement = await pending_movement(db)

    async with db() as session:
        with pytest.raises(SpendCapReached):
            await ingestion.enrich_movement(session, movement.id, llm=CappedLLM())

    async with db() as session:
        stored = await session.get(Movement, movement.id)
        assert (stored.news_status, stored.news_attempts) == (NewsStatus.PENDING, 0)


async def test_a_refused_tier_search_is_not_a_failed_search(db):
    movement = await pending_movement(db)
    async with db() as session:
        with pytest.raises(SpendCapReached):
            await ingestion.enrich_movement(
                session, movement.id, llm=StubLLM(), news_provider=CappedNews()
            )


async def test_a_refused_ingestion_keeps_its_prices_and_reports_the_refusal(db):
    async with db() as session:
        ticker = await ingestion.get_or_create_ticker(session, "CAPI")
        assert await ingestion.claim_ingestion(session, ticker)
        result = await ingestion.ingest_ticker(session, "CAPI", llm=CappedLLM())

    assert isinstance(result.deferred, SpendCapReached)
    assert any("News not fetched" in w for w in result.warnings)
    async with db() as session:
        ticker = await ingestion.get_ticker(session, "CAPI")
        assert ticker.ingest_status == IngestStatus.COMPLETE
        statuses = (
            await session.scalars(sa.select(Movement.news_status).where(Movement.ticker_id == ticker.id))
        ).all()
        assert statuses and set(statuses) == {NewsStatus.PENDING}


async def test_the_worker_holds_a_refused_job_until_the_reset(db):
    movement = await pending_movement(db)
    async with db() as session:
        run = await prewarm.start_run(session, date(2026, 10, 9), now=datetime.now(timezone.utc))
        job = await queue.enqueue(
            session, JobKind.ENRICH_MOVEMENT, {"movement_id": movement.id, "run_id": run.id},
            priority=40, dedupe_key=queue.enrich_key(movement.id), source=JobSource.SCHEDULED,
            run_id=run.id,
        )
        await session.commit()
    worker = Worker(session_factory=db, llm=CappedLLM())

    assert (await worker.run_once()).id == job.id

    async with db() as session:
        held = await session.get(Job, job.id)
        assert (held.status, held.attempts, held.hold_reason) == (
            JobStatus.QUEUED, 0, queue.HOLD_SPEND_CAP,
        )
        reset = spend.next_reset(datetime.now(timezone.utc))
        run_after = held.run_after.replace(tzinfo=timezone.utc)
        assert reset <= run_after <= reset + timedelta(seconds=settings.spend_resume_jitter_seconds)
        assert "cap reached" in held.last_error
        assert (await session.get(PrewarmRun, run.id)).enrichments_deferred == 1
        assert (await session.get(Movement, movement.id)).news_status == NewsStatus.PENDING

    assert await worker.run_once() is None, "held, not due again before midnight"


async def test_a_held_job_is_claimed_normally_after_the_reset(db, stub_llm):
    movement = await pending_movement(db)
    async with db() as session:
        await queue.enqueue(
            session, JobKind.ENRICH_MOVEMENT, {"movement_id": movement.id}, priority=40,
            dedupe_key=queue.enrich_key(movement.id), source=JobSource.SCHEDULED,
        )
        await session.commit()
    await Worker(session_factory=db, llm=CappedLLM()).run_once()

    async with db() as session:
        tomorrow = spend.next_reset(datetime.now(timezone.utc)) + timedelta(hours=1)
        job = await queue.claim(session, "w", now=tomorrow)
        assert job is not None and job.hold_reason is None and job.attempts == 1


@pytest.mark.parametrize(("priority", "expected"), [(0, CallClass.INTERACTIVE), (5, CallClass.BACKGROUND), (90, CallClass.BACKGROUND)])
def test_a_jobs_call_class_follows_its_priority(priority, expected):
    assert call_class_for(Job(priority=priority)) == expected


async def test_the_worker_attributes_spend_to_the_job(db, ledger, monkeypatch):
    seen: list[context.Attribution] = []

    async def handler(session, job, ctx) -> None:
        seen.append(context.current())
        await charge_as_is()

    async def charge_as_is() -> None:
        async with spend.metered("exa", "search", Decimal("0.01")) as meter:
            await meter.settle(cost_usd=Decimal("0.01"), estimated=False)

    async with db() as session:
        nightly = await queue.enqueue(session, JobKind.PREWARM_SECTOR_MACRO, {}, priority=10,
                                      dedupe_key="macro:x", source=JobSource.SCHEDULED)
        asked = await queue.enqueue(session, JobKind.PREWARM_SECTOR_MACRO, {}, priority=0,
                                    dedupe_key="macro:y", source=JobSource.INTERACTIVE)
        await session.commit()
    worker = Worker(session_factory=db, llm=StubLLM(), handlers={JobKind.PREWARM_SECTOR_MACRO: handler})
    await worker.run_once()
    await worker.run_once()

    rows = await ledger_rows(ledger)
    assert {(r.job_id, r.call_class) for r in rows} == {
        (asked.id, CallClass.INTERACTIVE),
        (nightly.id, CallClass.BACKGROUND),
    }
    assert context.current().job_id is None, "restored after the job"


# -------------------------------------------------------------------- API


@pytest.fixture
def capped_client(session_factory, user_key):
    """The API, signed in, with an LLM that is refused by the cap."""
    return api_client(api_app(session_factory, CappedChat()), user_key[1])


class CappedChat(StubLLM):
    async def complete(self, **kwargs: Any) -> str:
        raise SpendCapReached("interactive", spend.next_reset(datetime.now(timezone.utc)), "Daily cap reached.")

    async def parse_structured(self, **kwargs: Any) -> Any:
        raise SpendCapReached("interactive", spend.next_reset(datetime.now(timezone.utc)), "Daily cap reached.")


async def test_chat_over_the_cap_is_503_with_retry_after(capped_client, session_factory):
    from app.models.chat import ChatMessage

    async with capped_client as client:
        response = await client.post("/chat", json={"question": "Why did it move?"})

    assert response.status_code == 503
    assert response.json()["error"] == "spend_cap_reached"
    retry_after = int(response.headers["retry-after"])
    assert 0 < retry_after <= 24 * 3600
    async with session_factory() as session:
        assert (await session.scalar(sa.select(sa.func.count()).select_from(ChatMessage))) == 0


async def test_a_capped_wait_request_still_serves_the_stored_data(capped_client):
    async with capped_client as client:
        response = await client.get("/tickers/CAPW", params={"wait": True})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["movements"], "prices and movements are stored and shown"
    assert any("News not fetched" in w for w in body["warnings"])


async def test_queued_work_warns_once_users_are_being_refused(client, session_factory, monkeypatch):
    cap(monkeypatch, 10.0)
    async with session_factory() as session:
        session.add(
            SpendDaily(
                day=spend.utc_day(datetime.now(timezone.utc)), spent_usd=Decimal(10),
                background_spent_usd=Decimal(0), reserved_usd=Decimal(0),
                background_reserved_usd=Decimal(0), updated_at=datetime.now(timezone.utc),
                interactive_cap_logged_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    body = (await client.get("/tickers/LATER")).json()

    assert body["job_id"] is not None
    assert any("spend cap" in w for w in body["warnings"])


async def test_api_spend_is_attributed_to_the_user_as_interactive(session_factory, ledger, user_key):
    class MeteredChat(StubLLM):
        async def complete(self, **kwargs: Any) -> str:
            async with spend.metered("anthropic", kwargs["operation"], Decimal("0.01")) as meter:
                await meter.settle(cost_usd=Decimal("0.004"), estimated=False)
            return "An answer."

    user, key = user_key
    async with api_client(api_app(session_factory, MeteredChat()), key) as client:
        assert (await client.post("/chat", json={"question": "Anything new?"})).status_code == 200

    (row,) = await ledger_rows(ledger)
    assert (row.operation, row.call_class, row.job_id, row.user_id) == (
        "chat", CallClass.INTERACTIVE, None, user.id,
    )


# ------------------------------------------------------------------ admin


async def test_admin_usage_shows_where_the_money_went(client, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "s3cret")
    cap(monkeypatch, 10.0, share=0.5)
    now = datetime.now(timezone.utc)
    rows = [
        ("exa", "search", "0.010", True, CallClass.BACKGROUND, None),
        ("exa", "search", "0.020", False, CallClass.INTERACTIVE, 1),
        ("anthropic", "relevance", "0.500", False, CallClass.BACKGROUND, None),
        ("anthropic", "chat", "0.250", False, CallClass.INTERACTIVE, 2),
        ("anthropic", "chat", "0.100", False, CallClass.INTERACTIVE, 1),
    ]
    async with session_factory() as session:
        for provider, operation, cost, estimated, call_class, user_id in rows:
            session.add(UsageEvent(
                created_at=now, provider=provider, operation=operation, cost_usd=Decimal(cost),
                cost_estimated=estimated, call_class=call_class, user_id=user_id,
            ))
        session.add(UsageEvent(  # yesterday: not counted
            created_at=now - timedelta(days=1), provider="exa", operation="search",
            cost_usd=Decimal(5), cost_estimated=False, call_class=CallClass.BACKGROUND,
        ))
        session.add(SpendDaily(
            day=spend.utc_day(now), spent_usd=Decimal("0.88"), background_spent_usd=Decimal("0.51"),
            reserved_usd=Decimal("0.12"), background_reserved_usd=Decimal("0.02"), updated_at=now,
        ))
        await session.commit()

    body = (await client.get("/admin/usage", headers={"X-Admin-Token": "s3cret"})).json()

    assert Decimal(body["total_usd"]) == Decimal("0.88")
    assert Decimal(body["background_usd"]) == Decimal("0.51")
    assert body["calls"] == 5
    top = body["by_operation"][0]
    assert (top["provider"], top["operation"], Decimal(top["cost_usd"])) == ("anthropic", "relevance", Decimal("0.5"))
    search = next(r for r in body["by_operation"] if r["operation"] == "search")
    assert (search["calls"], search["estimated_calls"]) == (2, 1)
    assert [(u["user_id"], Decimal(u["cost_usd"])) for u in body["top_users"]] == [
        (2, Decimal("0.25")), (1, Decimal("0.12")),
    ]
    assert Decimal(body["cap_usd"]) == 10 and Decimal(body["background_cap_usd"]) == 5
    assert Decimal(body["headroom_usd"]) == Decimal("9")  # 10 - 0.88 - 0.12
    assert Decimal(body["background_headroom_usd"]) == Decimal("4.47")  # 5 - 0.51 - 0.02

    yesterday = (now - timedelta(days=1)).date().isoformat()
    past = (await client.get("/admin/usage", params={"date": yesterday}, headers={"X-Admin-Token": "s3cret"})).json()
    assert Decimal(past["total_usd"]) == 5


async def test_admin_queue_counts_jobs_held_by_the_cap(client, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "s3cret")
    async with session_factory() as session:
        for key, reason in (("a", queue.HOLD_SPEND_CAP), ("b", queue.HOLD_SPEND_CAP), ("c", None)):
            job = await queue.enqueue(session, JobKind.ENRICH_MOVEMENT, {}, priority=40,
                                      dedupe_key=key, source=JobSource.SCHEDULED)
            job.hold_reason = reason
        await session.commit()

    body = (await client.get("/admin/queue", headers={"X-Admin-Token": "s3cret"})).json()
    assert body["held_by_spend_cap"] == 2
