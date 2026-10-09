"""Price history via yfinance.

yfinance is a scraper of an undocumented endpoint: it fails by returning an
empty frame at least as often as it raises, and it changes shape between
versions. Everything here is defensive, and the rest of the application only
ever sees `PricePoint` objects and `PriceDataError`.

Two ways in: `fetch_price_history` for one ticker on demand, and
`fetch_price_histories` for the nightly refresh, which batch-downloads a
hundred symbols per call. Both share one frame parser, so they cannot drift.

Adjusted closes are revised retroactively. A split or dividend rescales every
earlier adjusted close, so bars stored last month and bars fetched today can
sit on different bases. `merge_price_bars` detects that on a day the two
overlap and rebases the stored history before the series are joined; without
it, a 2-for-1 split would appear as a -50% movement at the seam.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import yfinance as yf

from app.core.config import settings
from app.core.errors import PriceDataError, TickerNotFoundError
from app.core.logging import get_logger
from app.services import ratelimit
from app.services.movements import PricePoint

# One history request and one profile request.
YF_CALLS_PER_SINGLE_FETCH = 2

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TickerProfile:
    """Company metadata. `sector`/`industry` ground the competitor tier."""

    symbol: str
    company_name: str | None = None
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class PriceHistory:
    profile: TickerProfile
    bars: list["PriceBarData"]


@dataclass(frozen=True, slots=True)
class PriceBarData:
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float
    volume: int | None

    def to_point(self) -> PricePoint:
        return PricePoint(date=self.date, adj_close=self.adj_close, volume=self.volume)


def _clean_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def _clean_int(value: Any) -> int | None:
    out = _clean_float(value)
    return None if out is None else int(out)


# yfinance signals "this symbol does not exist" and "the network broke" through
# the same exception type, distinguishable only by the message. Getting this
# wrong means an unknown ticker returns 502 (upstream broken) instead of 404
# (you asked for something that is not there), which is a materially worse
# answer for the caller.
_NOT_FOUND_MARKERS = (
    "possibly delisted",
    "no timezone found",
    "no data found",
    "symbol may be delisted",
    "no price data found",
)


def _looks_like_unknown_symbol(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def _column(row: pd.Series, *names: str) -> Any:
    for name in names:
        if name in row.index:
            return row[name]
    return None


def _start_date(days: int) -> date:
    return (datetime.now(timezone.utc) - timedelta(days=days)).date()


def _fetch_sync(symbol: str, days: int) -> PriceHistory:
    """Blocking yfinance work. Runs in a worker thread, never on the loop."""
    ticker = yf.Ticker(symbol)
    start = _start_date(days)

    try:
        # auto_adjust=False keeps raw OHLC *and* gives us an explicit
        # "Adj Close" column, which is what returns must be computed from --
        # using raw close would invent a -30% "movement" on every split.
        frame = ticker.history(
            start=start.isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=False,
            raise_errors=True,
        )
    except Exception as exc:  # yfinance raises a grab-bag of exception types
        if _looks_like_unknown_symbol(str(exc)):
            raise TickerNotFoundError(symbol) from exc
        raise PriceDataError(
            "yfinance", f"history fetch failed for {symbol}: {exc}"
        ) from exc

    bars = _bars_from_frame(frame, symbol)  # before the profile: it costs a call
    return PriceHistory(profile=_fetch_profile(ticker, symbol), bars=bars)


def _bars_from_frame(frame: pd.DataFrame | None, symbol: str) -> list[PriceBarData]:
    """One symbol's daily bars from a yfinance frame.

    Raises TickerNotFoundError for an empty frame (an unknown symbol and a
    delisted one look identical here) and PriceDataError when rows exist but
    none is usable.
    """
    if frame is None:
        raise TickerNotFoundError(symbol)
    if isinstance(frame.columns, pd.MultiIndex):
        frame = frame.copy()
        frame.columns = frame.columns.get_level_values(-1)
    # A batch download pads every symbol to the union of all dates, so a
    # symbol with no data arrives as rows of NaN rather than as no rows.
    frame = frame.dropna(how="all")
    if frame.empty:
        raise TickerNotFoundError(symbol)

    bars: list[PriceBarData] = []
    for index, row in frame.iterrows():
        adj_close = _clean_float(_column(row, "Adj Close", "Close"))
        if adj_close is None or adj_close <= 0:
            continue
        bar_date = index.date() if hasattr(index, "date") else index
        bars.append(
            PriceBarData(
                date=bar_date,
                open=_clean_float(_column(row, "Open")),
                high=_clean_float(_column(row, "High")),
                low=_clean_float(_column(row, "Low")),
                close=_clean_float(_column(row, "Close")),
                adj_close=adj_close,
                volume=_clean_int(_column(row, "Volume")),
            )
        )

    if not bars:
        raise PriceDataError("yfinance", f"no usable price bars for {symbol}")
    return bars


def _fetch_profile(ticker: "yf.Ticker", symbol: str) -> TickerProfile:
    """Best-effort metadata. Never fatal: prices matter, the profile is a bonus."""
    info: dict[str, Any] = {}
    try:
        info = ticker.get_info() or {}
    except Exception as exc:  # pragma: no cover - network-dependent
        logger.warning("ticker_profile_unavailable", symbol=symbol, error=str(exc))

    return TickerProfile(
        symbol=symbol.upper(),
        company_name=info.get("longName") or info.get("shortName"),
        sector=info.get("sector"),
        industry=info.get("industry"),
        exchange=info.get("fullExchangeName") or info.get("exchange"),
        currency=info.get("currency"),
    )


async def fetch_price_history(symbol: str, days: int | None = None) -> PriceHistory:
    """Fetch daily bars and company metadata for `symbol`.

    Raises:
        TickerNotFoundError: the symbol returned no data at all.
        PriceDataError: the fetch failed or returned nothing usable.
    """
    days = days or settings.price_history_days
    symbol = symbol.strip().upper()
    logger.info("price_fetch_start", symbol=symbol, days=days)
    for _ in range(YF_CALLS_PER_SINGLE_FETCH):
        await ratelimit.bucket("yfinance").acquire()

    history = await asyncio.to_thread(_fetch_sync, symbol, days)

    logger.info(
        "price_fetch_complete",
        symbol=symbol,
        bars=len(history.bars),
        first=str(history.bars[0].date),
        last=str(history.bars[-1].date),
    )
    return history


# ------------------------------------------------------------ batch fetch


async def fetch_price_histories(
    symbols: list[str],
    days: int,
    *,
    with_profile: Iterable[str] = (),
) -> dict[str, PriceHistory | Exception]:
    """Daily bars for many symbols, in batches of `PRICE_BATCH_SIZE`.

    Each symbol maps to its history or to the error that symbol alone hit, so
    one delisted name never costs the rest of its batch. The company profile
    is one HTTP call per symbol, so it is fetched only for `with_profile`
    (tickers with nothing stored yet); everyone else gets an empty profile,
    which `_apply_profile` leaves the stored values untouched for.
    """
    symbols = sorted({symbol.strip().upper() for symbol in symbols})
    wanted_profiles = {symbol.strip().upper() for symbol in with_profile}
    results: dict[str, PriceHistory | Exception] = {}

    for offset in range(0, len(symbols), settings.price_batch_size):
        chunk = symbols[offset : offset + settings.price_batch_size]
        logger.info("price_batch_fetch_start", symbols=len(chunk), days=days)
        await ratelimit.bucket("yfinance").acquire()
        try:
            frame, errors = await asyncio.to_thread(_download_sync, chunk, days)
        except Exception as exc:  # the whole batch failed: every symbol is transient
            failure = PriceDataError("yfinance", f"batch download failed: {exc}")
            results.update({symbol: failure for symbol in chunk})
            continue

        for symbol, outcome in split_batch_frame(frame, chunk, errors).items():
            if isinstance(outcome, Exception):
                results[symbol] = outcome
                continue
            if symbol in wanted_profiles:
                await ratelimit.bucket("yfinance").acquire()
                profile = await asyncio.to_thread(_fetch_profile, yf.Ticker(symbol), symbol)
            else:
                profile = TickerProfile(symbol=symbol)
            results[symbol] = PriceHistory(profile=profile, bars=outcome)

        failed = sorted(s for s in chunk if isinstance(results[s], Exception))
        logger.info(
            "price_batch_fetch_complete",
            symbols=len(chunk),
            failed=len(failed),
            failed_symbols=failed[:20],
        )
    return results


def split_batch_frame(
    frame: pd.DataFrame | None, symbols: list[str], errors: dict[str, str]
) -> dict[str, list[PriceBarData] | Exception]:
    """Split a `group_by="ticker"` batch frame into per-symbol bars.

    `errors` is what yfinance recorded per symbol. An error that reads like an
    unknown symbol, or no data and no error at all, is TickerNotFoundError;
    any other recorded error is a transient PriceDataError, so a network blip
    on one symbol is retried rather than mistaken for a delisting.
    """
    present = (
        set(frame.columns.get_level_values(0))
        if frame is not None and isinstance(frame.columns, pd.MultiIndex)
        else set()
    )
    out: dict[str, list[PriceBarData] | Exception] = {}
    for symbol in symbols:
        error = errors.get(symbol)
        if error and not _looks_like_unknown_symbol(error):
            out[symbol] = PriceDataError("yfinance", f"{symbol}: {error}")
            continue
        try:
            sub_frame = frame[symbol] if symbol in present else None
            out[symbol] = _bars_from_frame(sub_frame, symbol)
        except (TickerNotFoundError, PriceDataError) as exc:
            out[symbol] = exc
    return out


def _download_sync(symbols: list[str], days: int) -> tuple[pd.DataFrame, dict[str, str]]:
    """One blocking batch download, plus the per-symbol errors yfinance saw.

    `yf.download` only logs per-symbol errors, so the public function cannot
    tell an unknown symbol from a timeout. Its implementation takes a context
    object that collects them; calling that directly gets them back. It is
    internal to yfinance (pinned <2), so if it moves, this falls back to the
    public function and reports every empty symbol as not found -- the same
    rule the single-symbol path uses.
    """
    kwargs: dict[str, Any] = {
        "start": _start_date(days).isoformat(),
        "interval": "1d",
        "group_by": "ticker",
        "auto_adjust": False,  # keep "Adj Close"; see _fetch_sync
        "actions": False,
        "threads": True,
        "progress": False,
        "multi_level_index": True,
    }
    impl = getattr(yf.multi, "_download_impl", None)
    context_cls = getattr(yf.multi, "_DownloadCtx", None)
    if impl is not None and context_cls is not None:
        context = context_cls()
        frame = impl(context, symbols, **kwargs)
        errors = {str(k).upper(): str(v) for k, v in getattr(context, "errors", {}).items()}
        return frame, errors
    return yf.download(tickers=symbols, **kwargs), {}


# ------------------------------------------------------------------- merge

# Relative difference on an overlapping day above which the stored and fresh
# series are taken to be on different adjustment bases. Stored prices round to
# six decimals; the smallest real dividend adjustment is far larger than that.
BASIS_TOLERANCE = 1e-4


@dataclass(frozen=True, slots=True)
class MergedBars:
    bars: list[PriceBarData]
    # Factor the stored history before the fresh window was rescaled by, if
    # a split or dividend had changed the basis since it was stored.
    rebased_by: float | None = None
    # False when the fresh window does not reach back to the stored bars, so
    # the basis could not be checked.
    overlapped: bool = True


def merge_price_bars(
    stored: list[PriceBarData], fresh: list[PriceBarData]
) -> MergedBars:
    """Stored history joined with a freshly fetched trailing window.

    Fresh bars replace stored ones on the same date. Stored bars before the
    fresh window are kept, rebased if needed: on the oldest fresh day that is
    also stored, the ratio of fresh to stored adjusted close is the factor
    every earlier adjusted close has since been revised by. Raw OHLC and
    volume are split-adjusted by yfinance as well, so they get the same
    treatment using the raw close.
    """
    if not fresh:
        return MergedBars(bars=sorted(stored, key=lambda b: b.date))
    fresh_by_date = {bar.date: bar for bar in fresh}
    first_fresh = min(fresh_by_date)
    older = sorted((b for b in stored if b.date < first_fresh), key=lambda b: b.date)
    overlap = sorted((b for b in stored if b.date in fresh_by_date), key=lambda b: b.date)

    rebased_by: float | None = None
    if older and overlap:
        anchor = overlap[0]
        current = fresh_by_date[anchor.date]
        adj_ratio = current.adj_close / anchor.adj_close
        raw_ratio = (
            current.close / anchor.close
            if current.close and anchor.close
            else 1.0
        )
        if abs(adj_ratio - 1) > BASIS_TOLERANCE or abs(raw_ratio - 1) > BASIS_TOLERANCE:
            older = [_rescale(bar, adj_ratio, raw_ratio) for bar in older]
            rebased_by = adj_ratio

    merged = older + sorted(fresh_by_date.values(), key=lambda b: b.date)
    return MergedBars(bars=merged, rebased_by=rebased_by, overlapped=bool(overlap) or not older)


def _rescale(bar: PriceBarData, adj_ratio: float, raw_ratio: float) -> PriceBarData:
    def scale(value: float | None) -> float | None:
        return None if value is None else value * raw_ratio

    return replace(
        bar,
        open=scale(bar.open),
        high=scale(bar.high),
        low=scale(bar.low),
        close=scale(bar.close),
        adj_close=bar.adj_close * adj_ratio,
        # A split multiplies the share count by what it divides the price by.
        volume=None if bar.volume is None else round(bar.volume / raw_ratio),
    )
