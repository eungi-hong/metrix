"""The ticker endpoint's v2 fields and filters, and the analytics backfill
endpoint. Additive: everything v1 returned is still there (test_api.py)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.models.enums import IngestStatus, NewsStatus, RelevanceTier
from app.models.jobs import Job, JobKind
from app.models.market import Movement
from app.models.news import MovementNewsLink, NewsArticle, url_fingerprint
from app.services import ingestion
from tests import finance_helpers as fh
from tests.test_finance_ingestion import COMPANY_DAY, SHOCKS, store_benchmarks

ADMIN = {"X-Admin-Token": "s3cret"}


async def add_link(session, movement: Movement, url: str, *, published: datetime | None = None,
                   timing: str | None = None, superseded: bool = False, tier=RelevanceTier.EASY) -> None:
    article = NewsArticle(url_hash=url_fingerprint(url), url=url, title=url, provider="test", published_at=published)
    session.add(article)
    await session.flush()
    session.add(
        MovementNewsLink(
            movement_id=movement.id, article_id=article.id, relevance_tier=tier, relevance_score=0.8,
            timing=timing, timing_precision="time" if timing else None,
            superseded_at=datetime.now(timezone.utc) if superseded else None,
        )
    )


@pytest.fixture
async def ready(session_factory):
    """SYNTH stored, fresh, every movement researched; one movement no longer
    qualifies; the company-day move has a current and a superseded link."""
    data = fh.factor_histories(300, shocks=SHOCKS)
    async with session_factory() as session:
        await store_benchmarks(session, data)
        ticker = await ingestion.get_or_create_ticker(session, "SYNTH")
        await ingestion.refresh_prices(session, ticker, data.stock)
        ticker.ingest_status = IngestStatus.COMPLETE
        ticker.last_ingested_at = datetime.now(timezone.utc)
        movements = (await session.scalars(sa.select(Movement).order_by(Movement.date))).all()
        for m in movements:
            m.news_status = NewsStatus.COMPLETE
        company = next(m for m in movements if m.date == data.dates[COMPANY_DAY])
        await add_link(session, company, "https://n.test/current", timing="overnight",
                       published=datetime(2026, 1, 1, 22, tzinfo=timezone.utc))
        await add_link(session, company, "https://n.test/stale", superseded=True, tier=RelevanceTier.HARD)
        retired = movements[0]
        retired.qualifies = False
        await session.commit()
        return data, company.date, retired.date


async def test_a_movement_has_nested_decomposition_legs_volume_drift_and_regime(client, ready):
    data, company_day, _ = ready
    body = (await client.get("/tickers/SYNTH")).json()
    move = next(m for m in body["movements"] if m["date"] == company_day.isoformat())

    d = move["decomposition"]
    assert (d["model"], d["market_benchmark"], d["sector_benchmark"], d["dominant"]) == ("market_sector", "SPY", "XLK", "company")
    assert d["alpha"] + d["market"] + d["sector"] + d["abnormal"] == pytest.approx(move["daily_return"], abs=1e-12)
    assert move["trigger"] == "both" and move["qualifies"] is True
    assert (1 + move["legs"]["gap"]) * (1 + move["legs"]["intraday"]) == pytest.approx(1 + move["daily_return"])
    assert move["volume_analysis"]["relative_volume"] == pytest.approx(3.0)
    assert move["volume"] == fh.BASE_VOLUME * 3  # v1's field, unchanged
    assert set(move["drift"]) == {"car_5", "car_20", "label_5", "label_20"}
    assert move["regime"]["window_sessions"] == settings.regime_window_sessions
    assert move["decomposition_warning"] is None


async def test_superseded_links_are_hidden_and_current_ones_carry_timing(client, ready):
    _, company_day, _ = ready
    body = (await client.get("/tickers/SYNTH")).json()
    move = next(m for m in body["movements"] if m["date"] == company_day.isoformat())

    assert [link["article"]["url"] for link in move["news"]] == ["https://n.test/current"]
    assert (move["news"][0]["timing"], move["news"][0]["timing_precision"]) == ("overnight", "time")
    # A tier filter does not find a movement through a superseded link either.
    hard = (await client.get("/tickers/SYNTH", params={"tier": "hard"})).json()
    assert hard["movements"] == []


async def test_movements_kept_only_for_their_research_are_hidden_unless_asked_for(client, ready):
    _, _, retired_day = ready
    default = (await client.get("/tickers/SYNTH", params={"limit": 200})).json()
    assert retired_day.isoformat() not in {m["date"] for m in default["movements"]}

    everything = (await client.get("/tickers/SYNTH", params={"limit": 200, "include_unqualified": True})).json()
    retired = next(m for m in everything["movements"] if m["date"] == retired_day.isoformat())
    assert retired["qualifies"] is False
    assert everything["pagination"]["total"] == default["pagination"]["total"] + 1


async def test_trigger_dominant_and_sar_filters(client, ready):
    both = (await client.get("/tickers/SYNTH", params={"trigger": "both", "limit": 200})).json()
    assert both["movements"] and {m["trigger"] for m in both["movements"]} == {"both"}
    assert both["filters"]["triggers"] == ["both"]

    company = (await client.get("/tickers/SYNTH", params={"dominant": "company", "limit": 200})).json()
    assert company["movements"] and {m["decomposition"]["dominant"] for m in company["movements"]} == {"company"}

    big = (await client.get("/tickers/SYNTH", params={"min_sar": 3, "limit": 200})).json()
    assert big["movements"] and all(abs(m["decomposition"]["sar"]) >= 3 for m in big["movements"])


@pytest.mark.parametrize(
    ("sort", "key"),
    [
        ("abs_sar", lambda m: abs(m["decomposition"]["sar"]) if m["decomposition"] and m["decomposition"]["sar"] is not None else -1),
        ("abs_return", lambda m: abs(m["daily_return"])),
        ("rvol", lambda m: m["volume_analysis"]["relative_volume"] if m["volume_analysis"] else -1),
    ],
)
async def test_sorting_largest_first(client, ready, sort, key):
    movements = (await client.get("/tickers/SYNTH", params={"sort": sort, "limit": 200})).json()["movements"]
    values = [key(m) for m in movements]
    assert values == sorted(values, reverse=True)


async def test_benchmarks_come_rebased_to_100_for_an_overlay(client, ready):
    data, _, _ = ready
    start = data.dates[100]
    body = (await client.get("/tickers/SYNTH", params={"include_benchmarks": True, "start": start.isoformat()})).json()

    by_role = {series["role"]: series for series in body["benchmarks"]}
    assert (by_role["market"]["symbol"], by_role["sector"]["symbol"]) == ("SPY", "XLK")
    for series in by_role.values():
        assert series["base_date"] == start.isoformat()
        assert series["points"][0]["value"] == 100.0
        assert len(series["points"]) == 200
    assert (await client.get("/tickers/SYNTH")).json()["benchmarks"] is None


async def test_the_admin_backfill_queues_one_job_per_stored_ticker(client, ready, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "s3cret")
    response = await client.post("/admin/backfill-analytics", headers=ADMIN)

    assert response.status_code == 202
    assert response.json()["symbols"] == ["SYNTH"]  # SPY and XLK are benchmarks
    again = await client.post("/admin/backfill-analytics", headers=ADMIN)
    assert again.json()["queued"] == 1
    async with session_factory() as session:
        jobs = (await session.scalars(sa.select(Job).where(Job.kind == JobKind.BACKFILL_ANALYTICS))).all()
    assert [job.payload for job in jobs] == [{"symbol": "SYNTH"}]  # deduplicated while active


async def test_chat_leaves_out_retired_movements_and_superseded_links(client, ready):
    _, company_day, retired_day = ready
    body = (await client.post("/chat", json={"ticker": "SYNTH", "question": "What moved it?"})).json()

    dates = {source["date"] for source in body["sources"]["movements"]}
    urls = {source["url"] for source in body["sources"]["articles"]}
    assert company_day.isoformat() in dates and retired_day.isoformat() not in dates
    assert urls == {"https://n.test/current"}
