"""Synthetic market, sector and stock histories with known factor structure.

Built on real XNYS sessions so the session-timing code sees real dates. The
returns follow the decomposition model exactly, with known parameters:

    r_m        market, N(MARKET_DRIFT, MARKET_VOL)
    u          the sector's own part, N(0, SECTOR_OWN_VOL)
    r_s        = SECTOR_ALPHA + SECTOR_BETA * r_m + u
    e          the stock's own part, N(0, STOCK_OWN_VOL)
    r_i        = ALPHA + BETA_M * r_m + BETA_S * u + e

`shocks` overrides (r_m, u, e) on chosen session indices, to plant a market,
sector or company day. Synthetic: not market data, and labelled so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from app.services import sessions
from app.services.prices import PriceBarData, PriceHistory, TickerProfile

MARKET_DRIFT = 0.0003
MARKET_VOL = 0.010
SECTOR_ALPHA = 0.0001
SECTOR_BETA = 1.05
SECTOR_OWN_VOL = 0.007
ALPHA = 0.0004
BETA_M = 1.2
BETA_S = 0.9
STOCK_OWN_VOL = 0.012
BASE_VOLUME = 1_000_000


@dataclass(frozen=True)
class FactorData:
    dates: list[date]
    stock: PriceHistory
    market: PriceHistory
    sector: PriceHistory


def xnys_sessions(start: date, count: int) -> list[date]:
    cal = sessions.calendar()
    first = cal.date_to_session(pd.Timestamp(start), direction="next")
    days = cal.sessions_window(first, count)
    return [d.date() for d in days]


def factor_histories(
    count: int = 300,
    *,
    start: date = date(2025, 1, 2),
    seed: int = 7,
    shocks: dict[int, tuple[float, float, float]] | None = None,
    symbol: str = "SYNTH",
    sector: str = "Technology",
    industry: str = "Software - Infrastructure",
) -> FactorData:
    rng = np.random.default_rng(seed)
    dates = xnys_sessions(start, count)
    r_m = rng.normal(MARKET_DRIFT, MARKET_VOL, count)
    u = rng.normal(0, SECTOR_OWN_VOL, count)
    e = rng.normal(0, STOCK_OWN_VOL, count)
    r_m[0] = u[0] = e[0] = 0.0  # the first bar has no return
    for index, (market, own_sector, own_stock) in (shocks or {}).items():
        r_m[index], u[index], e[index] = market, own_sector, own_stock
    r_s = SECTOR_ALPHA + SECTOR_BETA * r_m + u
    r_i = ALPHA + BETA_M * r_m + BETA_S * u + e
    r_s[0] = r_i[0] = 0.0

    def history(name: str, returns: np.ndarray, profile_sector: str | None, profile_industry: str | None) -> PriceHistory:
        closes = 100.0 * np.cumprod(1 + returns)
        bars = []
        for k, day in enumerate(dates):
            close = float(closes[k])
            previous = float(closes[k - 1]) if k else close
            opened = previous * (1 + returns[k] / 2)  # half the move at the open
            bars.append(
                PriceBarData(
                    date=day,
                    open=opened,
                    high=max(opened, close) * 1.002,
                    low=min(opened, close) * 0.998,
                    close=close,
                    adj_close=close,
                    volume=BASE_VOLUME * (3 if abs(returns[k]) > 0.03 else 1),
                )
            )
        return PriceHistory(
            profile=TickerProfile(
                symbol=name,
                company_name=f"{name} (synthetic)",
                sector=profile_sector,
                industry=profile_industry,
                exchange="NYSE",
                currency="USD",
            ),
            bars=bars,
        )

    return FactorData(
        dates=dates,
        stock=history(symbol, r_i, sector, industry),
        market=history("SPY", r_m, None, None),
        sector=history("XLK", r_s, None, None),
    )
