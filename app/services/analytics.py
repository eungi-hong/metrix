"""Per-movement analytics, from stored daily bars alone.

For each movement on session T (previous stock bar P), all computed from the
stock's own bars and its benchmarks' bars, no vendor call:

* Decomposition (`app.services.decomposition`), estimated on the
  `estimation_sessions` aligned returns that end at P.
* Legs. ``gap = O_T * (adj_close_T / close_T) / adj_close_P - 1`` (the open,
  put on the adjusted basis with that bar's own factor) and ``intraday =
  close_T / O_T - 1``, so ``(1 + gap) * (1 + intraday) = 1 + r`` exactly.
* Relative volume: ``V_T / median(V over the prior 20 sessions)``.
* Drift after the move: cumulative abnormal return over T+1..T+5 and
  T+1..T+20 with the model fitted at T, labelled `continued` or `reversed`
  when it has the same or the opposite sign as T's abnormal return and is at
  least half its size, `held` otherwise. None until those sessions exist.
* Volatility regime: annualised realised volatility of the 20 returns before
  T, and its percentile among the same measure over the past year (also
  ending at P, so the move does not rate its own regime).

Pure: bars in, numbers out. Anything that cannot be computed is None, with a
reason where the decomposition fell back.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol

import numpy as np

from app.services.decomposition import Decomposition, FactorModel, InsufficientData, decompose, fit
from app.services.detection import AbnormalReading

DriftLabel = Literal["continued", "held", "reversed"]

TRADING_DAYS_PER_YEAR = 252
DRIFT_SHORT_SESSIONS = 5
DRIFT_LONG_SESSIONS = 20
# A percentile needs a spread of history to mean anything: at least this many
# regime-window lengths of rolling values.
REGIME_MIN_HISTORY_MULTIPLE = 3

WARNING_MISSING_BENCHMARK = "benchmark bars are missing for this session or the one before it"


class BarLike(Protocol):
    date: date
    open: float | None
    close: float | None
    adj_close: float
    volume: int | None


@dataclass(frozen=True, slots=True)
class AnalyticsParams:
    estimation_sessions: int
    min_sessions: int
    winsor_z: float
    rvol_lookback_sessions: int
    drift_label_fraction: float
    regime_window_sessions: int
    regime_lookback_sessions: int


@dataclass(frozen=True, slots=True)
class AlignedReturns:
    """Daily returns of a stock and its benchmarks over their common dates.

    `dates[k]` is the later day of each pair and `previous[k]` the earlier,
    the same for every series, so each return spans the same interval.
    """

    dates: list[date]
    previous: list[date]
    stock: np.ndarray
    market: np.ndarray
    sector: np.ndarray | None
    index: dict[date, int]


@dataclass(frozen=True, slots=True)
class MovementAnalytics:
    decomposition: Decomposition | None
    decomposition_warning: str | None
    gap: float | None
    intraday: float | None
    relative_volume: float | None
    car_short: float | None
    car_long: float | None
    drift_short: DriftLabel | None
    drift_long: DriftLabel | None
    realized_vol: float | None
    realized_vol_percentile: float | None


def _valid(bars: Sequence[BarLike]) -> list[BarLike]:
    """Sorted by date, one per date, positive adjusted close only (as the detector)."""
    by_date = {bar.date: bar for bar in bars if bar.adj_close is not None and bar.adj_close > 0}
    return [by_date[day] for day in sorted(by_date)]


def previous_dates(bars: Sequence[BarLike]) -> dict[date, date]:
    """Each bar's date mapped to the date of the bar before it."""
    valid = _valid(bars)
    return {current.date: prior.date for prior, current in zip(valid, valid[1:])}


def align_returns(
    stock: Sequence[BarLike], market: Sequence[BarLike], sector: Sequence[BarLike] | None
) -> AlignedReturns:
    stock_close = {bar.date: float(bar.adj_close) for bar in _valid(stock)}
    market_close = {bar.date: float(bar.adj_close) for bar in _valid(market)}
    sector_close = {bar.date: float(bar.adj_close) for bar in _valid(sector)} if sector is not None else None

    common = set(stock_close) & set(market_close)
    if sector_close is not None:
        common &= set(sector_close)
    days = sorted(common)

    def returns(closes: dict[date, float]) -> np.ndarray:
        prices = np.array([closes[day] for day in days], dtype=float)
        return prices[1:] / prices[:-1] - 1 if prices.size > 1 else np.array([], dtype=float)

    dates = days[1:]
    return AlignedReturns(
        dates=dates,
        previous=days[:-1],
        stock=returns(stock_close),
        market=returns(market_close),
        sector=returns(sector_close) if sector_close is not None else None,
        index={day: k for k, day in enumerate(dates)},
    )


def decompose_on(
    aligned: AlignedReturns,
    day: date,
    stock_previous: date | None,
    params: AnalyticsParams,
) -> Decomposition | str:
    """The decomposition of `day`, or the reason there is none.

    `stock_previous` is the stock's own previous bar: if the aligned series
    skipped it (a benchmark has no bar there), the aligned return is not the
    stock's return for the day, so the day is not decomposed.
    """
    k = aligned.index.get(day)
    if k is None or stock_previous is None or aligned.previous[k] != stock_previous:
        return WARNING_MISSING_BENCHMARK
    start = max(0, k - params.estimation_sessions)
    try:
        model = fit(
            aligned.stock[start:k],
            aligned.market[start:k],
            aligned.sector[start:k] if aligned.sector is not None else None,
            winsor_z=params.winsor_z,
            min_sessions=params.min_sessions,
        )
    except InsufficientData as exc:
        return str(exc)
    sector = float(aligned.sector[k]) if aligned.sector is not None else None
    return decompose(model, float(aligned.stock[k]), float(aligned.market[k]), sector)


def abnormal_readings(
    aligned: AlignedReturns | None,
    bars: Sequence[BarLike],
    days: Iterable[date],
    params: AnalyticsParams,
) -> dict[date, AbnormalReading | None]:
    """The decomposition's reading (SAR and abnormal return) of each of `days`,
    for detection. None for a day that cannot be decomposed; empty without
    an alignment at all. Shared by ingestion and scripts/measure_detection.py,
    so the measured counts come from the code that flags movements."""
    if aligned is None:
        return {}
    previous = previous_dates(bars)
    readings: dict[date, AbnormalReading | None] = {}
    for day in days:
        outcome = decompose_on(aligned, day, previous.get(day), params)
        readings[day] = (
            None if isinstance(outcome, str) else AbnormalReading(sar=outcome.sar, abnormal=outcome.abnormal)
        )
    return readings


def cumulative_abnormal(
    aligned: AlignedReturns, model: FactorModel, day: date, horizon: int
) -> float | None:
    """Sum of abnormal returns over the `horizon` sessions after `day`, with
    the parameters fitted at `day`. None until they all exist."""
    k = aligned.index.get(day)
    if k is None or k + horizon >= len(aligned.dates):
        return None
    total = 0.0
    for j in range(k + 1, k + horizon + 1):
        sector = float(aligned.sector[j]) if aligned.sector is not None else None
        total += model.abnormal(float(aligned.stock[j]), float(aligned.market[j]), sector)
    return total


def drift_label(car: float | None, abnormal: float, fraction: float) -> DriftLabel | None:
    if car is None:
        return None
    if abs(car) < fraction * abs(abnormal) or abnormal == 0:
        return "held"
    return "continued" if (car > 0) == (abnormal > 0) else "reversed"


def legs(previous: BarLike | None, bar: BarLike | None) -> tuple[float | None, float | None]:
    """(gap, intraday) for `bar`, whose previous bar is `previous`."""
    if previous is None or bar is None:
        return None, None
    if not bar.open or not bar.close or bar.open <= 0 or bar.close <= 0:
        return None, None
    adjusted_open = float(bar.open) * (float(bar.adj_close) / float(bar.close))
    gap = adjusted_open / float(previous.adj_close) - 1
    intraday = float(bar.close) / float(bar.open) - 1
    return gap, intraday


def relative_volume(prior: Sequence[BarLike], bar: BarLike | None, lookback: int) -> float | None:
    """Today's volume over the median of the `lookback` sessions before it."""
    if bar is None or bar.volume is None or len(prior) < lookback:
        return None
    window = [b.volume for b in prior[-lookback:]]
    if any(v is None for v in window):
        return None
    median = statistics.median(v for v in window if v is not None)
    return bar.volume / median if median > 0 else None


def volatility_regime(
    returns_before: Sequence[float], window: int, lookback: int
) -> tuple[float | None, float | None]:
    """(annualised volatility of the last `window` returns, its percentile 0
    to 100 among the rolling values over the last `lookback` returns)."""
    if len(returns_before) < window:
        return None, None
    values = np.asarray(returns_before[-lookback:], dtype=float)
    annualise = math.sqrt(TRADING_DAYS_PER_YEAR)
    rolling = [
        float(np.std(values[end - window : end], ddof=1)) * annualise
        for end in range(window, values.size + 1)
    ]
    current = rolling[-1]
    if len(rolling) < REGIME_MIN_HISTORY_MULTIPLE * window:
        return current, None
    below_or_equal = sum(1 for value in rolling if value <= current)
    return current, 100.0 * below_or_equal / len(rolling)


def analyse(
    day: date,
    stock_bars: Sequence[BarLike],
    aligned: AlignedReturns | None,
    params: AnalyticsParams,
    *,
    unavailable_reason: str | None = None,
) -> MovementAnalytics:
    """Everything above for the movement on `day`.

    `aligned` is None when no decomposition is possible for the ticker at all
    (it is a benchmark, or the market benchmark has no stored bars); then
    `unavailable_reason` says why.
    """
    bars = _valid(stock_bars)
    position = {bar.date: i for i, bar in enumerate(bars)}
    i = position.get(day)
    bar = bars[i] if i is not None else None
    previous = bars[i - 1] if i is not None and i > 0 else None

    decomposition: Decomposition | None = None
    warning = unavailable_reason
    if aligned is not None:
        outcome = decompose_on(aligned, day, previous.date if previous else None, params)
        if isinstance(outcome, str):
            warning = outcome
        else:
            decomposition = outcome

    car_short = car_long = None
    if decomposition is not None and aligned is not None:
        car_short = cumulative_abnormal(aligned, decomposition.model, day, DRIFT_SHORT_SESSIONS)
        car_long = cumulative_abnormal(aligned, decomposition.model, day, DRIFT_LONG_SESSIONS)
    abnormal = decomposition.abnormal if decomposition is not None else 0.0

    gap, intraday = legs(previous, bar)
    prior = bars[:i] if i is not None else []
    stock_returns = [
        float(b.adj_close) / float(a.adj_close) - 1 for a, b in zip(prior, prior[1:])
    ]
    vol, percentile = volatility_regime(
        stock_returns, params.regime_window_sessions, params.regime_lookback_sessions
    )
    return MovementAnalytics(
        decomposition=decomposition,
        decomposition_warning=warning,
        gap=gap,
        intraday=intraday,
        relative_volume=relative_volume(prior, bar, params.rvol_lookback_sessions),
        car_short=car_short,
        car_long=car_long,
        drift_short=drift_label(car_short, abnormal, params.drift_label_fraction)
        if decomposition is not None
        else None,
        drift_long=drift_label(car_long, abnormal, params.drift_label_fraction)
        if decomposition is not None
        else None,
        realized_vol=vol,
        realized_vol_percentile=percentile,
    )
