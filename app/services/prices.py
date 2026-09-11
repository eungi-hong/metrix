"""Price history via yfinance.

yfinance is a scraper of an undocumented endpoint: it fails by returning an
empty frame at least as often as it raises, and it changes shape between
versions. Everything here is defensive, and the rest of the application only
ever sees `PricePoint` objects and `PriceDataError`.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import yfinance as yf

from app.core.config import settings
from app.core.errors import PriceDataError, TickerNotFoundError
from app.core.logging import get_logger
from app.services.movements import PricePoint

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


def _fetch_sync(symbol: str, days: int) -> PriceHistory:
    """Blocking yfinance work. Runs in a worker thread, never on the loop."""
    ticker = yf.Ticker(symbol)
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date()

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

    if frame is None or frame.empty:
        # An unknown symbol and a delisted one look identical here.
        raise TickerNotFoundError(symbol)

    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)

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

    return PriceHistory(profile=_fetch_profile(ticker, symbol), bars=bars)


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

    history = await asyncio.to_thread(_fetch_sync, symbol, days)

    logger.info(
        "price_fetch_complete",
        symbol=symbol,
        bars=len(history.bars),
        first=str(history.bars[0].date),
        last=str(history.bars[-1].date),
    )
    return history
