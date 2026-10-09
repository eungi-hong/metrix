"""Tests for demand tracking and the pre-warm universe.

Recording is one upsert on Postgres and read-modify-write elsewhere, so these
run on both when Postgres is available, like the queue tests.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.models.demand import TickerDemand
from app.models.enums import IngestStatus
from app.models.market import Ticker
from app.services import demand
from app.services.demand import UniverseEntry, decayed_popularity
from tests.conftest import POSTGRES, postgres_session_factory, sqlite_session_factory

T0 = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
WEEK = timedelta(days=7)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=POSTGRES)])
async def session_factory(request):
    factory = sqlite_session_factory if request.param == "sqlite" else postgres_session_factory
    async with factory() as made:
        yield made


@pytest.fixture(autouse=True)
def demand_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "demand_half_life_days", 7.0)
    monkeypatch.setattr(settings, "prewarm_seed_symbols", "")
    monkeypatch.setattr(settings, "prewarm_seed_file", str(tmp_path / "none.txt"))
    monkeypatch.setattr(settings, "prewarm_top_n", 200)
    monkeypatch.setattr(settings, "prewarm_recent_days", 14)


async def hit(session, symbol: str, at: datetime, times: int = 1) -> None:
    for _ in range(times):
        await demand.record_demand(session, symbol, now=at)
    await session.commit()


async def stored(session, symbol: str) -> TickerDemand:
    row = await session.get(TickerDemand, symbol, populate_existing=True)
    assert row is not None
    return row


# ------------------------------------------------------------------ math


def test_a_hit_is_worth_half_a_half_life_later():
    assert decayed_popularity(1.0, T0, T0 + WEEK) == pytest.approx(0.5)
    assert decayed_popularity(1.0, T0, T0 + 2 * WEEK) == pytest.approx(0.25)
    assert decayed_popularity(3.0, T0, T0) == 3.0


def test_the_time_constant_is_half_life_over_ln_2():
    assert demand.decay_time_constant_days(7.0) == pytest.approx(7.0 / math.log(2))


def test_decay_never_inflates_a_score_from_the_future():
    assert decayed_popularity(2.0, T0 + timedelta(hours=1), T0) == 2.0


def test_daily_hits_converge_to_the_steady_state():
    score, at = 0.0, T0
    for day in range(200):
        at = T0 + timedelta(days=day)
        score = decayed_popularity(score, at - timedelta(days=1), at) + 1
    tau = demand.decay_time_constant_days(7.0)
    assert score == pytest.approx(1 / (1 - math.exp(-1 / tau)), rel=1e-6)


# ------------------------------------------------------------- recording


async def test_the_first_hit_creates_the_row(session):
    await hit(session, " nvda ", T0)

    row = await stored(session, "NVDA")
    assert row.popularity == 1.0
    assert row.request_count == 1
    assert row.first_requested_at.replace(tzinfo=timezone.utc) == T0
    assert row.last_requested_at.replace(tzinfo=timezone.utc) == T0


async def test_a_later_hit_decays_the_old_score_then_adds_one(session):
    await hit(session, "NVDA", T0, times=2)
    await hit(session, "NVDA", T0 + WEEK)

    row = await stored(session, "NVDA")
    assert row.popularity == pytest.approx(2 * 0.5 + 1)
    assert row.request_count == 3
    assert row.first_requested_at.replace(tzinfo=timezone.utc) == T0
    assert row.popularity_updated_at.replace(tzinfo=timezone.utc) == T0 + WEEK


async def test_a_failed_record_never_fails_the_caller(session, monkeypatch):
    session.add(Ticker(symbol="KEEP"))
    await session.flush()

    async def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(demand, "_record_portable", broken)
    monkeypatch.setattr(demand, "_record_postgres", broken)

    await demand.record_demand(session, "NVDA", now=T0)  # does not raise

    # The caller's own pending work survives the failed savepoint.
    await session.commit()
    assert await session.scalar(sa.select(Ticker.symbol)) == "KEEP"
    assert await session.get(TickerDemand, "NVDA") is None


# --------------------------------------------------------------- universe


def by_symbol(entries: list[UniverseEntry]) -> dict[str, UniverseEntry]:
    return {e.symbol: e for e in entries}


async def test_the_universe_is_seeds_plus_popular_plus_recent(session, monkeypatch, tmp_path):
    seed_file = tmp_path / "seeds.txt"
    seed_file.write_text("# large caps\nAAPL\nmsft   # lower case is fine\n\nNOT A SYMBOL\nAAPL\n")
    monkeypatch.setattr(settings, "prewarm_seed_file", str(seed_file))
    monkeypatch.setattr(settings, "prewarm_seed_symbols", "XOM, ,aapl")
    monkeypatch.setattr(settings, "prewarm_top_n", 2)
    monkeypatch.setattr(settings, "prewarm_recent_days", 14)
    now = T0 + 10 * WEEK

    await hit(session, "OLDHOT", T0, times=50)  # popular long ago, not recent
    await hit(session, "STEADY", now - timedelta(days=30), times=10)
    await hit(session, "FRESH", now - timedelta(days=1))
    await hit(session, "TAIL", now - timedelta(days=60))

    universe = await demand.select_prewarm_universe(session, now=now)
    entries = by_symbol(universe)

    # Seeds from both sources, deduplicated, invalid entry skipped.
    assert {"AAPL", "MSFT", "XOM"} <= set(entries)
    assert entries["AAPL"].seeded and entries["AAPL"].popularity == 0.0
    # Top 2 by popularity decayed to now: 10 hits 30 days ago (~0.51) beat
    # 50 hits 70 days ago (~0.05). FRESH is both top-2 and recent.
    assert entries["STEADY"].popularity == pytest.approx(10 * 0.5 ** (30 / 7))
    assert "OLDHOT" not in entries
    assert entries["FRESH"].recent
    # TAIL is neither popular enough nor recent.
    assert "TAIL" not in entries
    assert [e.symbol for e in universe][:2] == ["FRESH", "STEADY"]


async def test_recent_requests_are_included_beyond_the_top_n(session, monkeypatch):
    monkeypatch.setattr(settings, "prewarm_top_n", 1)
    await hit(session, "BIG", T0, times=20)
    await hit(session, "SMALL", T0)

    entries = by_symbol(await demand.select_prewarm_universe(session, now=T0 + timedelta(days=1)))

    assert set(entries) == {"BIG", "SMALL"}
    assert entries["SMALL"].recent


async def test_permanent_failures_are_excluded_even_if_seeded_or_popular(session, monkeypatch):
    monkeypatch.setattr(settings, "prewarm_seed_symbols", "GONE,FLAKY")
    session.add_all(
        [
            Ticker(symbol="GONE", ingest_status=IngestStatus.FAILED, ingest_error_permanent=True),
            Ticker(symbol="TYPO", ingest_status=IngestStatus.FAILED, ingest_error_permanent=True),
            Ticker(symbol="FLAKY", ingest_status=IngestStatus.FAILED, ingest_error_permanent=False),
        ]
    )
    await session.commit()
    await hit(session, "TYPO", T0, times=5)

    entries = by_symbol(await demand.select_prewarm_universe(session, now=T0))

    assert set(entries) == {"FLAKY"}, "a transient failure is still worth retrying"


async def test_a_weighted_hit_adds_its_weight_and_counts_as_one_request(session_factory):
    async with session_factory() as session:
        await demand.record_demand(session, "ANON", weight=0.25, now=T0)
        await demand.record_demand(session, "ANON", weight=0.25, now=T0 + WEEK)
        await session.commit()
        row = await stored(session, "ANON")

    assert row.request_count == 2
    assert row.popularity == pytest.approx(0.25 * 0.5 + 0.25)  # the first has halved
