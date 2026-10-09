"""Tests for the abuse controls on the expensive paths: the symbol directory
(parsing, symbol forms, refreshes that never empty it, enforce/warn/off),
demand that one caller cannot manufacture, and the refresh cooldown."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.models.demand import TickerDemand
from app.models.enums import IngestStatus
from app.models.identity import Plan
from app.models.jobs import Job, JobKind, JobStatus
from app.models.symbols import ListedSymbol
from app.services import auth, demand, ingestion, queue, symbol_directory
from app.services import prices as price_service
from app.services.symbol_directory import (
    DirectoryFormatError,
    DirectoryRefreshRefused,
    canonical,
    directory_form,
    parse,
)
from app.worker import Worker
from tests.conftest import StubLLM, api_app, api_client, build_price_history, new_api_key

FIXTURES = Path(__file__).parent / "fixtures"


def sample(name: str) -> str:
    return (FIXTURES / f"{name}_sample.txt").read_text()


async def samples() -> dict[str, str]:
    return {"nasdaqlisted": sample("nasdaqlisted"), "otherlisted": sample("otherlisted")}


@pytest.fixture
def small_directory(monkeypatch):
    """The checked-in samples are a few rows, not thousands."""
    monkeypatch.setattr(settings, "symbol_directory_min_rows", 1)


async def fill_directory(session_factory) -> None:
    async with session_factory() as session:
        await symbol_directory.refresh(session, fetch=samples)


@pytest.fixture
def external_calls(monkeypatch) -> list[str]:
    """Records every price fetch: the only external call a ticker request starts."""
    calls: list[str] = []

    async def fetch(symbol: str, days=None):
        calls.append(symbol)
        return build_price_history(symbol)

    monkeypatch.setattr(price_service, "fetch_price_history", fetch)
    return calls


async def demand_count(session_factory, symbol: str) -> int:
    async with session_factory() as session:
        row = await session.get(TickerDemand, symbol)
        return row.request_count if row else 0


async def jobs(session_factory) -> list[Job]:
    async with session_factory() as session:
        return list((await session.scalars(sa.select(Job))).all())


# ---------------------------------------------------------------- parsing


def test_the_samples_parse_and_test_issues_are_dropped():
    nasdaq = {listing.symbol: listing for listing in parse(sample("nasdaqlisted"), "nasdaqlisted")}
    other = {listing.symbol: listing for listing in parse(sample("otherlisted"), "otherlisted")}

    assert {"AAPL", "MSFT", "NVDA", "GOOGL", "QQQ"} <= set(nasdaq)
    assert "ZXYZ-A" not in nasdaq and "ZZZTX" not in other, "test issues"
    assert nasdaq["QQQ"].is_etf and not nasdaq["AAPL"].is_etf
    assert nasdaq["AAPL"].exchange == "Q" and other["JPM"].exchange == "N"
    assert other["SPY"].is_etf and other["SPY"].exchange == "P"
    assert other["BRK-B"].name.startswith("Berkshire Hathaway")


@pytest.mark.parametrize(
    ("in_file", "yahoo"),
    [("BRK.B", "BRK-B"), ("BRK.A", "BRK-A"), ("BF.B", "BF-B"), ("ABR$D", "ABR-PD"), ("AAPL", "AAPL")],
)
def test_symbol_forms_convert_both_ways(in_file, yahoo):
    assert canonical(in_file) == yahoo
    assert directory_form(yahoo) == in_file


def test_a_class_p_share_is_not_mistaken_for_a_preferred():
    assert directory_form("XYZ-P") == "XYZ.P"
    assert directory_form("ABR-PD") == "ABR$D"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("", "empty"),
        (sample("nasdaqlisted").rsplit("\n", 2)[0], "cut short"),  # no trailer line
        ("Ticker|Name\nAAPL|Apple\nFile Creation Time: 1|", "header lacks"),
    ],
)
def test_malformed_files_are_refused(text, problem):
    with pytest.raises(DirectoryFormatError, match=problem):
        parse(text, "nasdaqlisted")


# ---------------------------------------------------------------- refresh


async def test_a_refresh_fills_the_directory(session_factory, small_directory):
    await fill_directory(session_factory)
    async with session_factory() as session:
        symbols = set((await session.scalars(sa.select(ListedSymbol.symbol))).all())
    assert {"AAPL", "BRK-B", "SPY", "ABR-PD", "JPM"} <= symbols


async def test_a_failed_download_keeps_the_directory(session_factory, small_directory):
    await fill_directory(session_factory)

    async def down() -> dict[str, str]:
        raise OSError("nasdaqtrader.com unreachable")

    async with session_factory() as session:
        with pytest.raises(OSError):
            await symbol_directory.refresh(session, fetch=down)
        assert (await session.get(ListedSymbol, "AAPL")) is not None


async def test_a_truncated_download_keeps_the_directory(session_factory, small_directory):
    await fill_directory(session_factory)

    async def truncated() -> dict[str, str]:
        return {"nasdaqlisted": sample("nasdaqlisted")[:200], "otherlisted": sample("otherlisted")}

    async with session_factory() as session:
        with pytest.raises(DirectoryFormatError):
            await symbol_directory.refresh(session, fetch=truncated)
        assert (await session.get(ListedSymbol, "AAPL")) is not None


async def test_a_refresh_that_would_gut_the_directory_is_refused(session_factory, small_directory):
    await fill_directory(session_factory)

    async def nasdaq_only() -> dict[str, str]:  # otherlisted silently missing
        return {"nasdaqlisted": sample("nasdaqlisted")}

    async with session_factory() as session:
        with pytest.raises(DirectoryRefreshRefused):
            await symbol_directory.refresh(session, fetch=nasdaq_only)
        assert (await session.get(ListedSymbol, "JPM")) is not None


async def test_a_first_refresh_below_the_minimum_is_refused(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "symbol_directory_min_rows", 5000)
    async with session_factory() as session:
        with pytest.raises(DirectoryRefreshRefused):
            await symbol_directory.refresh(session, fetch=samples)
        assert (await session.scalar(sa.select(sa.func.count()).select_from(ListedSymbol))) == 0


async def test_the_refresh_is_scheduled_once_per_iso_week(db, stub_llm):
    worker = Worker(session_factory=db, llm=stub_llm)
    monday = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

    job = await worker.directory_tick(monday)
    assert job.kind == JobKind.REFRESH_SYMBOL_DIRECTORY and job.dedupe_key == "symbols:2026-W41"
    assert await worker.directory_tick(monday + timedelta(days=3)) is None, "once per ISO week"
    next_week = await worker.directory_tick(monday + timedelta(days=7))
    assert next_week.dedupe_key == "symbols:2026-W42"


async def test_the_refresh_job_fills_the_directory(db, stub_llm, small_directory, monkeypatch):
    async def fetched(session, **kwargs):
        return await REAL_REFRESH(session, fetch=samples)

    monkeypatch.setattr(symbol_directory, "refresh", fetched)
    worker = Worker(session_factory=db, llm=stub_llm)
    await worker.directory_tick(datetime.now(timezone.utc) - timedelta(minutes=5))
    async with db() as session:  # due now, not after the jitter
        await session.execute(sa.update(Job).values(run_after=datetime.now(timezone.utc) - timedelta(minutes=5)))
        await session.commit()

    ran = await worker.run_once()

    async with db() as session:
        assert (await session.get(Job, ran.id)).status == JobStatus.SUCCEEDED
        assert (await session.get(ListedSymbol, "BRK-B")) is not None


REAL_REFRESH = symbol_directory.refresh


# ------------------------------------------------------- enforce at the API


async def test_an_unknown_symbol_is_404_and_costs_nothing(client, session_factory, small_directory, external_calls):
    await fill_directory(session_factory)

    response = await client.get("/tickers/QWZX")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"
    assert "not a listed US symbol" in response.json()["detail"]
    assert external_calls == []
    assert await jobs(session_factory) == []
    assert await demand_count(session_factory, "QWZX") == 0


async def test_a_listed_symbol_is_served(client, session_factory, small_directory):
    await fill_directory(session_factory)
    assert (await client.get("/tickers/JPM")).status_code == 202


async def test_a_class_share_in_the_files_form_is_served_in_yahoos(client, session_factory, small_directory):
    await fill_directory(session_factory)

    body = (await client.get("/tickers/BRK.B")).json()

    assert body["ticker"]["symbol"] == "BRK-B"
    async with session_factory() as session:
        job = await queue.active_job(session, queue.ingest_key("BRK-B"))
    assert job is not None and job.payload["symbol"] == "BRK-B"


@pytest.mark.parametrize("reason", ["allowlist", "seed", "ingested"])
async def test_the_exceptions_to_enforcement(client, session_factory, small_directory, monkeypatch, reason):
    await fill_directory(session_factory)
    if reason == "allowlist":
        monkeypatch.setattr(settings, "symbol_allowlist", "RY.TO, vod.l")
        symbol = "RY.TO"
    elif reason == "seed":
        monkeypatch.setattr(settings, "prewarm_seed_symbols", "SEEDY")
        symbol = "SEEDY"
    else:
        symbol = "OLDCO"  # delisted since, but we have its history
        async with session_factory() as session:
            ticker = await ingestion.get_or_create_ticker(session, symbol)
            ticker.ingest_status = IngestStatus.COMPLETE
            ticker.last_ingested_at = datetime.now(timezone.utc)
            await session.commit()

    assert (await client.get(f"/tickers/{symbol}")).status_code in (200, 202)


async def test_a_permanently_failed_symbol_is_not_an_exception(client, session_factory, small_directory):
    await fill_directory(session_factory)
    async with session_factory() as session:
        ticker = await ingestion.get_or_create_ticker(session, "GONE")
        ticker.last_ingested_at = datetime.now(timezone.utc)
        ticker.ingest_error_permanent = True
        await session.commit()
    assert (await client.get("/tickers/GONE")).status_code == 404


async def test_an_empty_directory_warns_instead_of_enforcing(client, session_factory, external_calls):
    response = await client.get("/tickers/QWZX", params={"wait": True})

    assert response.status_code == 200
    assert any("not in the US symbol directory" in w for w in response.json()["warnings"])
    assert external_calls == ["QWZX"]


async def test_warn_and_off_modes(client, session_factory, small_directory, monkeypatch):
    await fill_directory(session_factory)
    monkeypatch.setattr(settings, "symbol_directory_mode", "warn")
    warned = await client.get("/tickers/QWZX")
    monkeypatch.setattr(settings, "symbol_directory_mode", "off")
    unchecked = await client.get("/tickers/QWZY")

    assert warned.status_code == 202 and any("directory" in w for w in warned.json()["warnings"])
    assert unchecked.status_code == 202 and unchecked.json()["warnings"] == []


# ------------------------------------------------------------------ demand


async def test_one_caller_hammering_a_symbol_adds_one_hit_a_day(client, session_factory):
    for _ in range(25):
        await client.get("/tickers/PUMP")

    async with session_factory() as session:
        row = await session.get(TickerDemand, "PUMP")
    assert row.request_count == 1 and row.popularity == pytest.approx(1.0)


async def test_the_next_day_counts_again(session_factory, monkeypatch):
    principal = auth.Principal(plan=Plan.FREE, client_ip="1.1.1.1", user_id=1)
    today = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    async with session_factory() as session:
        assert await demand.count_request(session, "PUMP", principal, now=today)
        assert not await demand.count_request(session, "PUMP", principal, now=today + timedelta(hours=11))
        assert await demand.count_request(session, "PUMP", principal, now=today + timedelta(days=1))
        await session.commit()
        assert (await session.get(TickerDemand, "PUMP")).request_count == 2


@pytest.mark.parametrize(("plan", "weight"), [(Plan.ANONYMOUS, 0.25), (Plan.FREE, 1.0), (Plan.PRO, 1.0)])
async def test_demand_is_weighted_by_plan(session_factory, plan, weight):
    principal = auth.Principal(plan=plan, client_ip="2.2.2.2", user_id=None if plan == Plan.ANONYMOUS else 9)
    async with session_factory() as session:
        await demand.count_request(session, "WGHT", principal)
        await session.commit()
        assert (await session.get(TickerDemand, "WGHT")).popularity == pytest.approx(weight)


async def test_internal_traffic_is_not_demand(session_factory):
    principal = auth.Principal(plan=Plan.INTERNAL, client_ip="3.3.3.3", user_id=4)
    async with session_factory() as session:
        assert not await demand.count_request(session, "OURS", principal)
        await session.commit()
        assert await session.get(TickerDemand, "OURS") is None


async def test_a_request_answered_429_is_not_demand(session_factory, monkeypatch):
    monkeypatch.setitem(
        settings.plan_limits_json,
        "free",
        settings.plan_limits_json["free"].model_copy(update={"cold_ingests_per_day": 0}),
    )
    _, key = await new_api_key(session_factory, plan=Plan.FREE)
    async with api_client(api_app(session_factory, StubLLM()), key) as client:
        assert (await client.get("/tickers/NOPE")).status_code == 429
    assert await demand_count(session_factory, "NOPE") == 0


async def test_spamming_does_not_reach_the_prewarm_universe(session_factory, monkeypatch):
    """Twenty-five requests from one caller against one each from three."""
    monkeypatch.setattr(settings, "prewarm_top_n", 1)
    monkeypatch.setattr(settings, "prewarm_recent_days", 0)
    monkeypatch.setattr(settings, "prewarm_seed_symbols", "")
    monkeypatch.setattr(settings, "prewarm_seed_file", "")
    spammer = auth.Principal(plan=Plan.FREE, client_ip="6.6.6.6", user_id=66)
    async with session_factory() as session:
        for _ in range(25):
            await demand.count_request(session, "SPAM", spammer)
        for user in (1, 2, 3):
            await demand.count_request(session, "REAL", auth.Principal(Plan.FREE, "1.1.1.1", user_id=user))
        await session.commit()

        universe = await demand.select_prewarm_universe(session, now=datetime.now(timezone.utc))

    assert [entry.symbol for entry in universe] == ["REAL"]


# ---------------------------------------------------------- refresh cooldown


async def test_a_refresh_inside_the_cooldown_serves_stored_data(client, session_factory, external_calls, monkeypatch):
    monkeypatch.setattr(settings, "refresh_cooldown_minutes", 60)
    async with session_factory() as session:
        ticker = await ingestion.get_or_create_ticker(session, "COOL")
        await ingestion.refresh_prices(session, ticker, build_price_history("COOL"))
        ticker.ingest_status = IngestStatus.COMPLETE
        ticker.last_ingested_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await session.execute(sa.text("UPDATE movements SET news_status = 'complete'"))
        await session.commit()

    body = (await client.get("/tickers/COOL", params={"refresh": True})).json()

    assert body["status"] == "ready" and body["movements"]
    assert any("available again in 50 minute(s)" in w for w in body["warnings"])
    assert external_calls == []
    assert await jobs(session_factory) == []


async def test_a_refresh_after_the_cooldown_runs(client, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "refresh_cooldown_minutes", 60)
    async with session_factory() as session:
        ticker = await ingestion.get_or_create_ticker(session, "WARMED")
        ticker.ingest_status = IngestStatus.COMPLETE
        ticker.last_ingested_at = datetime.now(timezone.utc) - timedelta(minutes=61)
        await session.commit()

    body = (await client.get("/tickers/WARMED", params={"refresh": True})).json()
    assert body["status"] == "refreshing" and body["job_id"]
    assert body["warnings"] == []
