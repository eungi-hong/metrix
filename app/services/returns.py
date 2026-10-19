"""Returns over sessions, from stored daily bars."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market import PriceBar, Ticker
from app.services import sessions

# Bars this many calendar days back are enough to find the previous session's close.
PREVIOUS_BAR_LOOKBACK = timedelta(days=10)


async def session_returns(
    session: AsyncSession, symbols: list[str], day: date, *, sessions_back: int = 1
) -> dict[str, float | None]:
    """Each symbol's compounded close-to-close return over the `sessions_back`
    sessions ending on `day`, from stored bars; None where they are missing."""
    if not symbols:
        return {}
    start = sessions.offset_session(day, -sessions_back) if sessions.is_session(day) else None
    since = (start or day) - PREVIOUS_BAR_LOOKBACK
    rows = (
        await session.execute(
            sa.select(Ticker.symbol, PriceBar.date, PriceBar.adj_close)
            .join(PriceBar, PriceBar.ticker_id == Ticker.id)
            .where(Ticker.symbol.in_(symbols), PriceBar.date >= since, PriceBar.date <= day)
            .order_by(Ticker.symbol, PriceBar.date)
        )
    ).all()
    bars: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for symbol, bar_date, close in rows:
        bars[symbol].append((bar_date, float(close)))
    out: dict[str, float | None] = {}
    for symbol in symbols:
        series = bars.get(symbol, [])
        last = next((close for d, close in reversed(series) if d == day), None)
        base_day = start if start is not None else None
        base = next((close for d, close in reversed(series) if base_day is not None and d <= base_day), None)
        out[symbol] = (last / base - 1.0) if last and base else None
    return out
