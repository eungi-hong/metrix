"""Pending-evidence status, the latest session's priority, and peers'
prices in the nightly run."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.models.enums import NewsStatus
from app.models.jobs import Job, JobKind, JobStatus
from app.models.market import Movement, PriceBar
from app.services import demand, evidence, ingestion, prewarm, queue, schedule
from app.services import prices as price_service
from tests.test_finance_api import ready  # noqa: F401  (fixture)
from tests.test_finance_ingestion import COMPANY_DAY, data  # noqa: F401  (fixture)

NOW = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)  # a Wednesday
CLOSES = datetime(2026, 3, 6, 21, 0, tzinfo=timezone.utc)


def movement(**kw) -> SimpleNamespace:
    base = dict(news_status=NewsStatus.PENDING, news_window_closes_at=CLOSES, evidence_abstained=None,
                abstain_reason=None, news_attempts=0)
    return SimpleNamespace(**(base | kw))


def job(status="queued", hold=None) -> evidence.ActiveJob:
    return evidence.ActiveJob(status, hold, NOW + timedelta(hours=1))


@pytest.mark.parametrize(
    ("kw", "has_evidence", "active", "state"),
    [
        ({}, True, None, "explained"),
        ({"evidence_abstained": True, "abstain_reason": "Nothing fits."}, False, None, "abstained"),
        ({"news_status": NewsStatus.COMPLETE}, False, None, "no_evidence"),
        ({}, False, job("running"), "researching"),
        ({}, False, job(hold=queue.HOLD_SPEND_CAP), "budget_deferred"),
        ({"news_status": NewsStatus.PARTIAL}, False, job(), "window_open"),
        ({}, False, job(), "queued"),
        ({"news_status": NewsStatus.PARTIAL}, False, None, "window_open"),
        ({"news_status": NewsStatus.FAILED, "news_attempts": 1}, False, None, "retrying"),
        ({"news_status": NewsStatus.FAILED, "news_attempts": 99}, False, None, "failed"),
        ({}, False, None, "not_started"),
    ],
)
def test_each_state(kw, has_evidence, active, state):
    status = evidence.status_for(movement(**kw), has_evidence=has_evidence, job=active, now=NOW)
    assert status.state == state
    assert status.window_closes_at == CLOSES


def test_when_research_is_expected():
    held = evidence.status_for(movement(), has_evidence=False, job=job(hold=queue.HOLD_SPEND_CAP), now=NOW)
    assert held.expected_by == datetime(2026, 3, 5, tzinfo=timezone.utc)  # the cap resets at 00:00 UTC
    queued = evidence.status_for(movement(), has_evidence=False, job=job(), now=NOW)
    assert queued.expected_by == NOW + timedelta(hours=1)
    waiting = evidence.status_for(movement(), has_evidence=False, job=None, now=NOW)
    assert waiting.expected_by == schedule.next_run_after(NOW)
    partial = evidence.status_for(movement(news_status=NewsStatus.PARTIAL), has_evidence=False, job=None, now=NOW)
    assert partial.expected_by == schedule.next_run_after(CLOSES)  # after the window closes
    assert evidence.status_for(movement(), has_evidence=True, job=None, now=NOW).expected_by is None
    abstained = evidence.status_for(movement(evidence_abstained=True), has_evidence=False, job=None, now=NOW)
    assert abstained.detail == "Nothing found explains this move."


async def test_the_api_gives_every_movement_a_status(client, ready):  # noqa: F811
    _, company_day, _ = ready
    body = (await client.get("/tickers/SYNTH")).json()
    states = {m["date"]: m["evidence"]["state"] for m in body["movements"]}
    assert states[company_day.isoformat()] == "explained"
    assert set(states.values()) <= {"explained", "no_evidence"}  # the fixture marks them all COMPLETE


async def test_a_held_job_changes_the_status_and_the_etag(client, ready, session_factory):  # noqa: F811
    _, company_day, _ = ready
    async with session_factory() as session:
        quiet = await session.scalar(
            sa.select(Movement).where(Movement.date != company_day, Movement.qualifies.is_(True)).limit(1)
        )
        quiet.news_status = NewsStatus.PENDING
        await session.commit()
        quiet_day = quiet.date

    def state(response) -> str:
        return next(m for m in response.json()["movements"] if m["date"] == quiet_day.isoformat())["evidence"]["state"]

    first = await client.get("/tickers/SYNTH")  # a request queues the research it is owed
    assert state(first) == "queued"
    assert (await client.get("/tickers/SYNTH", headers={"If-None-Match": first.headers["ETag"]})).status_code == 304

    async with session_factory() as session:
        held = await session.scalar(sa.select(Job).where(Job.kind == JobKind.ENRICH_MOVEMENT))
        held.hold_reason = queue.HOLD_SPEND_CAP
        await session.commit()
    second = await client.get("/tickers/SYNTH", headers={"If-None-Match": first.headers["ETag"]})
    assert second.status_code == 200  # no data changed, but the job did
    assert state(second) == "budget_deferred"


# -- the nightly run ---------------------------------------------------------


def test_a_seed_tickers_latest_session_move_comes_first():
    latest = prewarm.enrichment_priority(0.0, 0.12, seeded=True, latest_session=True)
    assert latest == 15
    assert prewarm.enrichment_priority(0.0, 0.01, seeded=True, latest_session=True) == 18
    assert queue.PRIORITY_MACRO < latest < prewarm.enrichment_priority(600.0, 0.12, seeded=False)
    # Demand alone does not jump the queue, and an old move of a seed does not either.
    assert prewarm.enrichment_priority(600.0, 0.12, seeded=False, latest_session=True) >= 20
    assert prewarm.enrichment_priority(0.0, 0.12, seeded=True, latest_session=False) >= 60


async def test_the_chunk_prioritises_the_run_dates_moves(db, monkeypatch, data):  # noqa: F811
    feed = {"SYNTH": data.stock, "SPY": data.market, "XLK": data.sector}

    async def fetch_many(symbols, days, *, with_profile=()):
        return {symbol: feed[symbol] for symbol in symbols}

    monkeypatch.setattr(price_service, "fetch_price_histories", fetch_many)
    now = datetime.now(timezone.utc)
    trading_date = data.dates[COMPANY_DAY]
    async with db() as session:
        run = await prewarm.start_run(session, now.date(), now=now)
        await session.commit()
        await prewarm.refresh_chunk(
            session, [demand.UniverseEntry(symbol="SYNTH", popularity=0.0, seeded=True)], run.id,
            now=now, benchmark_symbols=["SPY", "XLK"], trading_date=trading_date,
        )
    async with db() as session:
        jobs = (await session.scalars(sa.select(Job).where(Job.kind == JobKind.ENRICH_MOVEMENT))).all()
        dates = {
            j.priority: (await session.get(Movement, j.payload["movement_id"])).date for j in jobs
        }
    assert dates[min(dates)] == trading_date and min(dates) < 20
    assert all(p >= 60 for p, d in dates.items() if d != trading_date)


async def test_peers_of_the_universe_are_listed_capped_and_deduplicated(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "prewarm_peer_symbols_max", 3)
    monkeypatch.setattr(settings, "symbol_directory_mode", "enforce")
    from app.models.symbols import ListedSymbol

    async with session_factory() as session:
        for symbol in ("AMD", "INTC", "AVGO", "QCOM", "TSM"):
            session.add(ListedSymbol(symbol=symbol, name=symbol, is_etf=False, source="nasdaqlisted",
                                     refreshed_at=NOW))
        nvda = await ingestion.get_or_create_ticker(session, "NVDA")
        nvda.peers = {"peers": [
            {"name": "AMD", "ticker": "AMD", "relationship": "competitor"},
            {"name": "Samsung", "ticker": "005930.KS", "relationship": "competitor"},  # not listed here
            {"name": "Intel", "ticker": "intc", "relationship": "competitor"},
            {"name": "Apple", "ticker": "AAPL", "relationship": "customer"},  # in the universe already
            {"name": "SPDR", "ticker": "SPY", "relationship": "partner"},  # a benchmark
            {"name": "Private Co", "ticker": None, "relationship": "supplier"},
        ]}
        amd = await ingestion.get_or_create_ticker(session, "AAPL")
        amd.peers = {"peers": [
            {"name": "AMD", "ticker": "AMD", "relationship": "competitor"},  # seen already
            {"name": "Qualcomm", "ticker": "QCOM", "relationship": "supplier"},
            {"name": "Broadcom", "ticker": "AVGO", "relationship": "supplier"},
        ]}
        await session.commit()
        chosen = await prewarm.peer_symbols(
            session, [demand.UniverseEntry("NVDA", 5.0), demand.UniverseEntry("AAPL", 1.0)]
        )
    assert chosen == ["AMD", "INTC", "QCOM"]


async def test_a_chunk_stores_peer_prices_only(db, monkeypatch, data):  # noqa: F811
    feed = {"SYNTH": data.stock, "PEER": data.sector}
    calls = []

    async def fetch_many(symbols, days, *, with_profile=()):
        calls.append((sorted(symbols), days))
        return {symbol: feed[symbol] for symbol in symbols}

    monkeypatch.setattr(price_service, "fetch_price_histories", fetch_many)
    now = datetime.now(timezone.utc)
    async with db() as session:
        run = await prewarm.start_run(session, now.date(), now=now)
        await session.commit()
        outcome = await prewarm.refresh_chunk(
            session, [demand.UniverseEntry(symbol="SYNTH", popularity=1.0)], run.id, now=now,
            peer_symbols=["PEER", "SYNTH"],
        )
    assert outcome.peers_refreshed == 1
    assert (["PEER"], settings.prewarm_price_lookback_days) in calls
    async with db() as session:
        peer = await ingestion.get_ticker(session, "PEER")
        assert not peer.is_benchmark and peer.last_ingested_at is None
        assert await session.scalar(sa.select(sa.func.count()).select_from(Movement).where(Movement.ticker_id == peer.id)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(PriceBar).where(PriceBar.ticker_id == peer.id)) > 0


async def test_the_nightly_plan_carries_the_peers_in_the_first_chunk(db, monkeypatch):
    async def fake_peers(session, universe):
        return ["AMD"]

    monkeypatch.setattr(prewarm, "peer_symbols", fake_peers)
    monkeypatch.setattr(settings, "prewarm_seed_symbols", "NVDA")
    today = date(2026, 3, 4)
    async with db() as session:
        await prewarm.schedule_nightly(session, today, now=NOW)
        (first,) = (await session.scalars(sa.select(Job).where(Job.kind == JobKind.REFRESH_PRICES))).all()
    assert first.payload["peers"] == ["AMD"] and first.payload["trading_date"] == today.isoformat()
    assert first.status == JobStatus.QUEUED
