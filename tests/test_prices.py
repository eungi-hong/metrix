"""Tests for batched price fetching, merging a short refresh window into stored
history, and `refresh_prices`. No network: yfinance is never called."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa
import yfinance as yf

from app.core.config import settings
from app.core.errors import PriceDataError, TickerNotFoundError
from app.models.market import PriceBar
from app.services import ingestion
from app.services import prices as price_service
from app.services.prices import (
    PriceBarData,
    PriceHistory,
    TickerProfile,
    merge_price_bars,
    split_batch_frame,
)
from tests.conftest import build_price_history

FIELDS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
DAYS = pd.to_datetime(["2026-07-01", "2026-07-02", "2026-07-03"])


def batch_frame(columns: dict[str, list[list[float]]]) -> pd.DataFrame:
    """What `yf.download(group_by="ticker")` returns: (Ticker, Price) columns,
    every symbol padded to the union of dates."""
    data = {
        (symbol, field): [row[i] for row in rows]
        for symbol, rows in columns.items()
        for i, field in enumerate(FIELDS)
    }
    frame = pd.DataFrame(data, index=DAYS)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["Ticker", "Price"])
    return frame


NAN_ROW = [np.nan] * 6


def good_rows(base: float) -> list[list[float]]:
    return [[base, base + 1, base - 1, base, base * 0.99, 1000.0] for _ in DAYS]


# ------------------------------------------------------ batch frame parsing


def test_a_batch_frame_splits_into_per_symbol_results():
    gappy = good_rows(50.0)
    gappy[1] = NAN_ROW  # a halted day: dropped, not fatal
    frame = batch_frame(
        {
            "AAA": good_rows(100.0),
            "GAP": gappy,
            "DEAD": [NAN_ROW] * 3,
            "SLOW": [NAN_ROW] * 3,
            "GONE": [NAN_ROW] * 3,
        }
    )
    errors = {
        "SLOW": "Timeout: read timed out",
        "GONE": "possibly delisted; no price data found",
    }

    out = split_batch_frame(frame, ["AAA", "GAP", "DEAD", "SLOW", "GONE", "MISSING"], errors)

    assert [b.adj_close for b in out["AAA"]] == pytest.approx([99.0] * 3)
    assert out["AAA"][0] == PriceBarData(
        date=date(2026, 7, 1), open=100.0, high=101.0, low=99.0,
        close=100.0, adj_close=99.0, volume=1000,
    )
    assert [b.date for b in out["GAP"]] == [date(2026, 7, 1), date(2026, 7, 3)]
    # No data and no error: indistinguishable from an unknown symbol.
    assert isinstance(out["DEAD"], TickerNotFoundError)
    # An error that is not about the symbol is transient, never "not found".
    assert isinstance(out["SLOW"], PriceDataError)
    assert not isinstance(out["SLOW"], TickerNotFoundError)
    assert isinstance(out["GONE"], TickerNotFoundError)
    assert isinstance(out["MISSING"], TickerNotFoundError)


def test_rows_without_a_usable_close_are_a_data_error():
    rows = [[1.0, 1.0, 1.0, np.nan, np.nan, 10.0]] * 3
    out = split_batch_frame(batch_frame({"BAD": rows}), ["BAD"], {})
    assert isinstance(out["BAD"], PriceDataError)


def test_the_internal_download_api_this_relies_on_still_exists():
    """`_download_sync` reads per-symbol errors through yfinance internals. If
    an upgrade moves them, this fails loudly instead of silently degrading."""
    assert callable(getattr(yf.multi, "_download_impl", None))
    assert hasattr(yf.multi._DownloadCtx(), "errors")


async def test_batches_are_chunked_and_profiles_fetched_only_when_asked(monkeypatch):
    monkeypatch.setattr(settings, "price_batch_size", 2)
    downloads: list[list[str]] = []
    profiled: list[str] = []

    def fake_download(symbols: list[str], days: int):
        downloads.append(symbols)
        return batch_frame({s: good_rows(10.0) for s in symbols if s != "BAD"}), {}

    def fake_profile(_ticker, symbol: str) -> TickerProfile:
        profiled.append(symbol)
        return TickerProfile(symbol=symbol, company_name=f"{symbol} Inc", sector="Tech")

    monkeypatch.setattr(price_service, "_download_sync", fake_download)
    monkeypatch.setattr(price_service, "_fetch_profile", fake_profile)
    monkeypatch.setattr(price_service.yf, "Ticker", lambda symbol: None)

    out = await price_service.fetch_price_histories(
        ["ccc", "AAA", "BAD", "DDD", "BBB"], days=45, with_profile=["bbb"]
    )

    assert downloads == [["AAA", "BAD"], ["BBB", "CCC"], ["DDD"]]
    assert profiled == ["BBB"]
    assert out["BBB"].profile.company_name == "BBB Inc"
    assert out["AAA"].profile == TickerProfile(symbol="AAA")
    assert isinstance(out["BAD"], TickerNotFoundError)
    assert len(out["DDD"].bars) == 3


async def test_a_failed_batch_fails_only_its_own_symbols_and_transiently(monkeypatch):
    monkeypatch.setattr(settings, "price_batch_size", 2)

    def flaky(symbols: list[str], days: int):
        if "AAA" in symbols:
            raise ConnectionError("reset by peer")
        return batch_frame({s: good_rows(10.0) for s in symbols}), {}

    monkeypatch.setattr(price_service, "_download_sync", flaky)

    out = await price_service.fetch_price_histories(["AAA", "BBB", "CCC"], days=45)

    assert isinstance(out["AAA"], PriceDataError) and isinstance(out["BBB"], PriceDataError)
    assert not isinstance(out["AAA"], TickerNotFoundError)
    assert isinstance(out["CCC"], PriceHistory)


# --------------------------------------------------------------------- merge


def bar(day: int, adj: float, close: float | None = None, volume: int = 1000) -> PriceBarData:
    close = adj if close is None else close
    return PriceBarData(
        date=date(2026, 1, 1) + timedelta(days=day),
        open=close, high=close, low=close, close=close, adj_close=adj, volume=volume,
    )


def test_fresh_bars_replace_stored_ones_and_older_ones_are_kept():
    stored = [bar(0, 10.0), bar(1, 11.0), bar(2, 12.0)]
    fresh = [bar(1, 11.0), bar(2, 12.5), bar(3, 13.0)]

    merged = merge_price_bars(stored, fresh)

    assert [b.adj_close for b in merged.bars] == [10.0, 11.0, 12.5, 13.0]
    assert merged.rebased_by is None
    assert merged.overlapped


def test_a_dividend_rebases_the_older_stored_history():
    """yfinance revised every earlier adjusted close by 0.99; raw closes did not move."""
    stored = [bar(0, 100.0), bar(1, 100.0), bar(2, 100.0)]
    fresh = [bar(1, 99.0, close=100.0), bar(2, 99.0, close=100.0)]

    merged = merge_price_bars(stored, fresh)

    assert merged.rebased_by == pytest.approx(0.99)
    assert merged.bars[0].adj_close == pytest.approx(99.0)
    assert merged.bars[0].close == pytest.approx(100.0)


def test_a_split_rebases_prices_and_volume_so_no_fake_move_appears():
    stored = [bar(0, 200.0, volume=1000), bar(1, 200.0, volume=1000)]
    fresh = [bar(1, 100.0, close=100.0, volume=2000), bar(2, 100.0, close=100.0, volume=2000)]

    merged = merge_price_bars(stored, fresh)

    assert [b.adj_close for b in merged.bars] == pytest.approx([100.0, 100.0, 100.0])
    assert merged.bars[0].close == pytest.approx(100.0)
    assert merged.bars[0].volume == 2000


def test_rounding_noise_does_not_trigger_a_rebase():
    stored = [bar(0, 123.456789), bar(1, 123.456789)]
    fresh = [bar(1, 123.4568), bar(2, 124.0)]
    assert merge_price_bars(stored, fresh).rebased_by is None


def test_a_fresh_window_that_misses_the_stored_bars_is_flagged():
    merged = merge_price_bars([bar(0, 10.0)], [bar(30, 12.0)])
    assert not merged.overlapped
    assert len(merged.bars) == 2


# ----------------------------------------------------------- refresh_prices


async def test_a_short_window_detects_with_the_full_rolling_window(session):
    """Ten fresh days hold too few returns for a 20-day volatility window on
    their own; merged with the stored history they give the same answer as a
    full fetch."""
    full = build_price_history("TEST")
    ticker = await ingestion.get_or_create_ticker(session, "TEST")
    first = await ingestion.refresh_prices(
        session, ticker, dataclasses.replace(full, bars=full.bars[:-1])
    )
    assert first.movements == []

    short = dataclasses.replace(full, profile=TickerProfile(symbol="TEST"), bars=full.bars[-10:])
    refreshed = await ingestion.refresh_prices(session, ticker, short)

    (movement,) = refreshed.movements
    assert refreshed.created == [movement]
    assert refreshed.unfinished == [movement]
    assert movement.threshold_source == "volatility"
    assert movement.rolling_std == pytest.approx(0.015, rel=0.05)
    # An empty profile leaves the stored one alone.
    assert ticker.company_name == "Test Industries Inc"


async def test_a_split_inside_the_short_window_does_not_create_a_movement(session):
    full = build_price_history("TEST")
    calm = full.bars[:-1]  # no shock day
    ticker = await ingestion.get_or_create_ticker(session, "TEST")
    await ingestion.refresh_prices(session, ticker, dataclasses.replace(full, bars=calm))

    # A 2-for-1 split since: yfinance now reports every price at half.
    halved = [
        dataclasses.replace(
            b, open=b.open / 2, high=b.high / 2, low=b.low / 2,
            close=b.close / 2, adj_close=b.adj_close / 2, volume=b.volume * 2,
        )
        for b in calm[-10:]
    ]
    refreshed = await ingestion.refresh_prices(
        session, ticker, dataclasses.replace(full, bars=halved)
    )

    assert refreshed.movements == []
    oldest = await session.scalar(
        sa.select(PriceBar).where(PriceBar.ticker_id == ticker.id).order_by(PriceBar.date)
    )
    assert float(oldest.adj_close) == pytest.approx(calm[0].adj_close / 2, rel=1e-5)


async def test_a_second_refresh_creates_nothing_new(session):
    history = build_price_history("TEST")
    ticker = await ingestion.get_or_create_ticker(session, "TEST")
    await ingestion.refresh_prices(session, ticker, history)

    again = await ingestion.refresh_prices(session, ticker, history)

    assert again.created == []
    assert len(again.movements) == 1
    assert again.bars_written == 0
