"""Volatility-adjusted detection of major price movements.

Definition
----------
A trading day is a *major movement* when::

    |daily_return| >= max(floor, k * rolling_std)

where ``daily_return`` is computed from adjusted close, and ``rolling_std`` is
the sample standard deviation of the ``window`` daily returns *immediately
preceding* the day under test.

Two deliberate choices:

1.  **The rolling window excludes the day being tested.** Including it would
    let a large move inflate its own threshold and mask itself -- a subtle
    lookahead bias that makes the most interesting days the least likely to be
    flagged. Every threshold here is computable from information available at
    the prior close.

2.  **The flat floor is a floor, not an alternative.** For a stock in a very
    quiet regime, 2 * sigma can be a few tens of basis points, which is noise
    rather than news. Taking the max keeps low-volatility names from flooding
    the pipeline, while the sigma term still catches a 3% day on a stock that
    normally moves 0.5% and correctly *ignores* a 3% day on a stock that moves
    4% routinely.

This module is intentionally pure: it takes prices, returns verdicts, touches
no database and no network, and is exhaustively unit tested.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date as Date

from app.core.config import Settings, settings as default_settings
from app.models.enums import Direction

# `threshold_source` values -- which term of the max() actually bound the day.
SOURCE_FLOOR = "floor"
SOURCE_VOLATILITY = "volatility"


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One adjusted-close observation. The detector's only input."""

    date: Date
    adj_close: float
    volume: int | None = None


@dataclass(frozen=True, slots=True)
class DetectionParams:
    """Detection parameters, captured per run so results stay auditable."""

    window: int = 20
    k: float = 2.0
    floor: float = 0.02

    @classmethod
    def from_settings(cls, s: Settings | None = None) -> "DetectionParams":
        s = s or default_settings
        return cls(
            window=s.movement_std_window,
            k=s.movement_k,
            floor=s.movement_floor_pct,
        )

    def __post_init__(self) -> None:
        if self.window < 2:
            raise ValueError("window must be >= 2 to have a standard deviation")
        if self.k <= 0:
            raise ValueError("k must be positive")
        if self.floor < 0:
            raise ValueError("floor must be non-negative")


@dataclass(frozen=True, slots=True)
class DailyReturn:
    """A day's return, with the threshold that was applied to it."""

    date: Date
    daily_return: float
    prev_adj_close: float
    adj_close: float
    volume: int | None
    rolling_std: float | None
    threshold: float
    threshold_source: str
    is_major: bool

    @property
    def abs_return(self) -> float:
        return abs(self.daily_return)

    @property
    def direction(self) -> Direction:
        return Direction.UP if self.daily_return >= 0 else Direction.DOWN

    @property
    def sigma_multiple(self) -> float | None:
        """How many standard deviations the move was. `None` without a window."""
        return sigma_multiple(self.daily_return, self.rolling_std)


def sigma_multiple(daily_return: float, rolling_std: float | None) -> float | None:
    """Size of a move in standard deviations, or None without a volatility window.

    Defined once here because both the detector's own value object and the API
    response report it, and two copies of a formula are one copy too many.
    """
    if not rolling_std:
        return None
    return abs(daily_return) / rolling_std


def _clean(prices: Iterable[PricePoint]) -> list[PricePoint]:
    """Sort by date, drop duplicates and unusable bars.

    A non-positive adjusted close is bad data, not a real price; leaving one in
    produces a division by zero or a nonsense return that would be flagged as
    the biggest movement in the series.
    """
    by_date: dict[Date, PricePoint] = {}
    for point in prices:
        if point.adj_close is None or point.adj_close <= 0:
            continue
        by_date[point.date] = point  # later observation wins
    return [by_date[d] for d in sorted(by_date)]


def compute_returns(
    prices: Iterable[PricePoint], params: DetectionParams | None = None
) -> list[DailyReturn]:
    """Compute every daily return with its volatility-adjusted threshold.

    Returns one entry per day that has a predecessor, in date order -- the
    first bar has no return and is omitted.
    """
    params = params or DetectionParams()
    points = _clean(prices)
    if len(points) < 2:
        return []

    results: list[DailyReturn] = []
    history: list[float] = []  # returns strictly before the current day

    for prev, current in zip(points, points[1:]):
        daily_return = (current.adj_close - prev.adj_close) / prev.adj_close

        rolling_std: float | None = None
        if len(history) >= params.window:
            rolling_std = statistics.stdev(history[-params.window :])

        vol_threshold = params.k * rolling_std if rolling_std is not None else 0.0
        if vol_threshold > params.floor:
            threshold, source = vol_threshold, SOURCE_VOLATILITY
        else:
            threshold, source = params.floor, SOURCE_FLOOR

        # `> 0` keeps a flat day from being flagged when the threshold is zero
        # (possible only with floor=0 and a perfectly flat window).
        is_major = abs(daily_return) >= threshold and abs(daily_return) > 0

        results.append(
            DailyReturn(
                date=current.date,
                daily_return=daily_return,
                prev_adj_close=prev.adj_close,
                adj_close=current.adj_close,
                volume=current.volume,
                rolling_std=rolling_std,
                threshold=threshold,
                threshold_source=source,
                is_major=is_major,
            )
        )
        history.append(daily_return)

    return results


def detect_movements(
    prices: Iterable[PricePoint], params: DetectionParams | None = None
) -> list[DailyReturn]:
    """The major-movement days only, in date order."""
    return [r for r in compute_returns(prices, params) if r.is_major]

