"""The finance core wired into ingestion: decomposition and analytics stored
on movements, detection modes, paid work kept rather than deleted, the
display window, link timing, the analytics backfill, and the nightly
benchmark refresh. Synthetic data with known structure (tests/finance_helpers),
SQLite, no network."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.models.enums import IngestStatus, NewsStatus, RelevanceTier
from app.models.jobs import Job, JobKind
from app.models.market import Movement, PriceBar, Ticker
from app.models.news import MovementNewsLink, NewsArticle, url_fingerprint
from app.services import demand, ingestion, prewarm
from app.services import prices as price_service
from app.services.news.base import NewsCandidate, NewsProvider, NewsSearchRequest
from app.services.news.queries import WINDOW_CALENDAR, WINDOW_SESSIONS
from tests import finance_helpers as fh

COMPANY_DAY = 250  # e = -9%: raw and abnormal
ABNORMAL_ONLY_DAY = 200  # market -2%, own +4.5%: r = +2.1%, under the raw bar
FLAT_DAY = 220  # sector -4%, own +3.6%: the stock is flat
SHOCKS = {
    COMPANY_DAY: (0.0, 0.0, -0.09),
    ABNORMAL_ONLY_DAY: (-0.02, 0.0, 0.045),
    FLAT_DAY: (0.0, -0.04, 0.036),
}


@pytest.fixture
def data() -> fh.FactorData:
    return fh.factor_histories(300, shocks=SHOCKS)


async def store_benchmarks(session, data: fh.FactorData, *, sector: bool = True) -> None:
    for history in (data.market, data.sector) if sector else (data.market,):
        ticker = await ingestion.get_or_create_ticker(session, history.profile.symbol)
        await ingestion.store_benchmark_prices(session, ticker, history)
    await session.commit()


async def refresh(session, data: fh.FactorData) -> ingestion.PriceRefresh:
    ticker = await ingestion.get_or_create_ticker(session, data.stock.profile.symbol)
    result = await ingestion.refresh_prices(session, ticker, data.stock)
    await session.commit()
    return result


async def movement_on(session, day: date) -> Movement | None:
    """The stored movement on `day`, read fresh from the database."""
    return await session.scalar(
        sa.select(Movement).where(Movement.date == day).execution_options(populate_existing=True)
    )


async def test_a_movement_carries_its_decomposition_and_analytics(session, data):
    await store_benchmarks(session, data)
    await refresh(session, data)

    m = await movement_on(session, data.dates[COMPANY_DAY])
    assert m is not None and m.qualifies and m.trigger == "both"
    assert (m.decomposition_model, m.market_benchmark, m.sector_benchmark) == ("market_sector", "SPY", "XLK")
    assert m.dominant_component == "company"
    assert m.alpha + m.market_component + m.sector_component + m.abnormal_return == pytest.approx(m.daily_return, abs=1e-12)
    assert m.sar is not None and m.sar < -2.5
    assert m.estimation_sessions == settings.decomposition_estimation_sessions
    assert (1 + m.gap_return) * (1 + m.intraday_return) == pytest.approx(1 + m.daily_return, rel=1e-9)
    assert m.relative_volume == pytest.approx(3.0)  # three times the usual volume
    assert m.car_5 is not None and m.car_20 is not None and m.drift_20_label in {"continued", "held", "reversed"}
    assert m.realized_vol is not None and m.analytics_computed_at is not None
    assert m.decomposition_warning is None


async def test_union_adds_a_day_that_only_stands_out_against_its_benchmarks(session, data, monkeypatch):
    await store_benchmarks(session, data)
    await refresh(session, data)
    assert await movement_on(session, data.dates[ABNORMAL_ONLY_DAY]) is None  # raw: not a movement

    monkeypatch.setattr(settings, "movement_detection_mode", "union")
    result = await refresh(session, data)

    m = await movement_on(session, data.dates[ABNORMAL_ONLY_DAY])
    assert m is not None and m.trigger == "abnormal" and m in result.created
    assert (await movement_on(session, data.dates[COMPANY_DAY])).trigger == "both"  # raw ones stay
    assert await movement_on(session, data.dates[FLAT_DAY]) is None  # |r| is under the floor


async def test_the_flat_day_needs_the_abnormal_floor_basis(session, data, monkeypatch):
    await store_benchmarks(session, data)
    monkeypatch.setattr(settings, "movement_detection_mode", "union")
    monkeypatch.setattr(settings, "movement_abnormal_floor_basis", "abnormal")
    await refresh(session, data)

    m = await movement_on(session, data.dates[FLAT_DAY])
    assert m is not None and m.trigger == "abnormal"
    assert abs(m.daily_return) < settings.movement_floor_pct
    assert m.dominant_component in {"sector", "company"}


async def link(session, movement: Movement, url: str) -> None:
    article = NewsArticle(url_hash=url_fingerprint(url), url=url, title=url, provider="test")
    session.add(article)
    await session.flush()
    session.add(
        MovementNewsLink(
            movement_id=movement.id, article_id=article.id,
            relevance_tier=RelevanceTier.EASY, relevance_score=0.9,
        )
    )
    await session.commit()


async def test_a_movement_that_stops_qualifying_is_kept_if_researched_and_deleted_if_not(session, data, monkeypatch):
    await store_benchmarks(session, data)
    result = await refresh(session, data)
    by_date = {m.date: m for m in result.movements}
    linked, enriched, untouched = sorted(by_date.values(), key=lambda m: m.date)[-3:]
    await link(session, linked, "https://n.test/linked")
    enriched.news_status = NewsStatus.COMPLETE  # researched, found nothing
    await session.commit()
    linked_day, enriched_day, untouched_day = linked.date, enriched.date, untouched.date

    monkeypatch.setattr(settings, "movement_k", 50.0)  # nothing clears the bar now
    monkeypatch.setattr(settings, "movement_floor_pct", 0.5)
    await refresh(session, data)

    kept_enriched = await movement_on(session, enriched_day)
    assert await movement_on(session, untouched_day) is None  # nothing paid for: deleted
    kept_linked = await movement_on(session, linked_day)
    assert kept_linked is not None and kept_linked.qualifies is False
    assert kept_enriched is not None and kept_enriched.qualifies is False
    assert await session.scalar(sa.select(sa.func.count()).select_from(MovementNewsLink)) == 1
    assert not ingestion.needs_enrichment(kept_linked, datetime.now(timezone.utc))
    owed = await ingestion.enrichable_movements(session, await ingestion.get_ticker(session, "SYNTH"), limit=100)
    assert kept_linked.id not in {m.id for m in owed}

    monkeypatch.setattr(settings, "movement_k", 2.0)  # back to the defaults
    monkeypatch.setattr(settings, "movement_floor_pct", 0.02)
    await refresh(session, data)
    assert (await movement_on(session, linked_day)).qualifies is True


async def test_new_movements_are_created_only_inside_the_display_window(session, data, monkeypatch):
    await store_benchmarks(session, data)
    monkeypatch.setattr(settings, "price_history_days", 60)
    await refresh(session, data)

    session.expire_all()
    dates = (await session.scalars(sa.select(Movement.date))).all()
    display_start = data.dates[-1] - timedelta(days=60)
    assert dates and min(dates) >= display_start

    # An older movement already stored stays a movement while it qualifies.
    monkeypatch.setattr(settings, "price_history_days", 365)
    await refresh(session, data)
    monkeypatch.setattr(settings, "price_history_days", 60)
    await refresh(session, data)
    session.expire_all()
    older = (await session.scalars(sa.select(Movement).where(Movement.date < display_start))).all()
    assert older and all(m.qualifies for m in older)


async def test_without_benchmarks_the_movement_says_why(session, data):
    await refresh(session, data)  # nothing stored for SPY
    m = await movement_on(session, data.dates[COMPANY_DAY])
    assert m.decomposition_model is None and m.trigger == "raw"
    assert "market benchmark SPY" in m.decomposition_warning
    assert m.gap_return is not None and m.relative_volume is not None  # needs no benchmark


async def test_a_missing_sector_benchmark_gives_a_market_only_model(session, data):
    await store_benchmarks(session, data, sector=False)
    await refresh(session, data)
    m = await movement_on(session, data.dates[COMPANY_DAY])
    assert m.decomposition_model == "market_only" and m.sector_benchmark is None
    assert "sector benchmark XLK" in m.decomposition_warning


async def test_a_benchmark_asked_for_as_a_ticker_is_not_decomposed(session, data):
    await store_benchmarks(session, data)
    spy = await ingestion.get_or_create_ticker(session, "SPY")
    shocked = fh.factor_histories(300, shocks={COMPANY_DAY: (-0.09, 0.0, 0.0)})
    await ingestion.refresh_prices(session, spy, shocked.market)
    await session.commit()
    m = await movement_on(session, data.dates[COMPANY_DAY])
    assert m is not None and m.decomposition_model is None
    assert "benchmark" in m.decomposition_warning


class TimedProvider(NewsProvider):
    """One company article, published at a fixed instant."""

    name = "timed"

    def __init__(self, published: datetime) -> None:
        self.published = published

    async def execute(self, request: NewsSearchRequest) -> dict[str, Any]:
        return {"results": [{"url": "https://n.test/timed"}] if "company news" in request.query else []}

    def parse(self, raw: dict[str, Any]) -> list[NewsCandidate]:
        return [
            NewsCandidate(url=item["url"], title="Timed", published_at=self.published, provider=self.name)
            for item in raw["results"]
        ]


async def test_a_link_records_when_its_article_was_published(session, data, stub_llm):
    await store_benchmarks(session, data)
    await refresh(session, data)
    m = await movement_on(session, data.dates[COMPANY_DAY])
    frame_day = data.dates[COMPANY_DAY - 1]
    evening_before = datetime.combine(frame_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=23)

    await ingestion.enrich_movement(session, m.id, llm=stub_llm, news_provider=TimedProvider(evening_before))

    stored = await session.scalar(sa.select(MovementNewsLink))
    assert (stored.timing, stored.timing_precision) == ("overnight", "time")


async def test_the_backfill_fills_analytics_without_touching_detection(session, data, monkeypatch):
    await store_benchmarks(session, data)
    result = await refresh(session, data)
    ticker = await ingestion.get_ticker(session, "SYNTH")
    for m in result.movements:  # as if stored before v2
        m.decomposition_model = m.sar = m.analytics_computed_at = m.car_5 = None
    await link(session, result.movements[0], "https://n.test/old")
    ticker.ingest_status, ticker.ingest_error = IngestStatus.FAILED, "price history unavailable"
    await session.commit()
    before = sorted(m.date for m in result.movements)

    fetches: list[int] = []

    async def deep_fetch(symbol: str, days: int | None = None) -> price_service.PriceHistory:
        fetches.append(days or 0)
        return data.stock

    monkeypatch.setattr(price_service, "fetch_price_history", deep_fetch)
    # "Today" is the day after the data ends, so 300 sessions (about 14
    # months) is shallower than a full fetch (display plus estimation days).
    the_next_day = datetime.combine(data.dates[-1] + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    monkeypatch.setattr(ingestion, "_now", lambda: the_next_day)
    outcome = await ingestion.backfill_analytics(session, ticker)

    session.expire_all()
    movements = (await session.scalars(sa.select(Movement).order_by(Movement.date))).all()
    assert [m.date for m in movements] == before  # nothing created or deleted
    assert all(m.analytics_computed_at is not None for m in movements)
    assert outcome.movements == len(before) and outcome.links_timed == 0  # date-less article
    assert fetches == [settings.price_fetch_days]  # 300 sessions is shallower than a full fetch
    ticker = await ingestion.get_ticker(session, "SYNTH")
    assert (ticker.ingest_status, ticker.ingest_error) == (IngestStatus.FAILED, "price history unavailable")


async def test_a_new_movement_takes_the_window_mode_and_an_enriched_one_keeps_its_own(session, data, stub_llm, monkeypatch):
    await store_benchmarks(session, data)
    result = await refresh(session, data)
    enriched = result.movements[0]
    enriched.news_status, enriched.news_attempts = NewsStatus.COMPLETE, 1
    await session.commit()
    assert enriched.news_window_mode == WINDOW_CALENDAR
    enriched_day, fresh_day = enriched.date, result.movements[-1].date

    monkeypatch.setattr(settings, "news_window_mode", "sessions")
    await refresh(session, data)

    kept = await movement_on(session, enriched_day)
    fresh = await movement_on(session, fresh_day)
    assert kept.news_window_mode == WINDOW_CALENDAR
    assert fresh.news_window_mode == WINDOW_SESSIONS


# ------------------------------------------------------------------ nightly


async def test_benchmarks_ride_in_the_first_chunk_and_the_rest_wait_for_it(db, monkeypatch):
    monkeypatch.setattr(settings, "prewarm_seed_symbols", "AAA,BBB,SPY")
    monkeypatch.setattr(settings, "prewarm_seed_file", "")
    monkeypatch.setattr(settings, "price_batch_size", 1)
    now = datetime.now(timezone.utc)

    async with db() as session:
        universe = await demand.select_prewarm_universe(session, now=now)
        assert [e.symbol for e in universe] == ["AAA", "BBB"]  # SPY is a benchmark, not a product
        run = await prewarm.schedule_nightly(session, now.date(), now=now)
        jobs = (await session.scalars(sa.select(Job).where(Job.kind == JobKind.REFRESH_PRICES).order_by(Job.id))).all()

    assert run is not None and len(jobs) == 2
    assert "SPY" in jobs[0].payload["benchmarks"] and jobs[0].blocked_by_key is None
    assert "benchmarks" not in jobs[1].payload and jobs[1].blocked_by_key == jobs[0].dedupe_key


async def test_a_chunk_stores_benchmark_prices_without_movements(db, monkeypatch, data):
    feed = {"SYNTH": data.stock, "SPY": data.market, "XLK": data.sector}

    async def fetch_many(symbols, days, *, with_profile=()):
        return {symbol: feed[symbol] for symbol in symbols}

    monkeypatch.setattr(price_service, "fetch_price_histories", fetch_many)
    now = datetime.now(timezone.utc)
    async with db() as session:
        run = await prewarm.start_run(session, now.date(), now=now)
        await session.commit()
        entry = demand.UniverseEntry(symbol="SYNTH", popularity=0.0, seeded=True, recent=False)
        outcome = await prewarm.refresh_chunk(
            session, [entry], run.id, now=now, benchmark_symbols=["SPY", "XLK"]
        )

    assert outcome.benchmarks_refreshed == 2 and outcome.refreshed == 1
    async with db() as session:
        spy = await ingestion.get_ticker(session, "SPY")
        assert spy.is_benchmark and spy.last_ingested_at is None
        assert await session.scalar(sa.select(sa.func.count()).select_from(Movement).where(Movement.ticker_id == spy.id)) == 0
        assert await session.scalar(sa.select(sa.func.count()).select_from(PriceBar).where(PriceBar.ticker_id == spy.id)) == 300
        m = await session.scalar(sa.select(Movement).where(Movement.date == data.dates[COMPANY_DAY]))
        assert m.decomposition_model == "market_sector"  # decomposed against today's benchmark bars
        assert (await session.get(Ticker, spy.id)).ingest_status == IngestStatus.PENDING
