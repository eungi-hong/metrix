"""Per-movement analytics from stored bars: legs, volume, drift, regime, and
the decomposition as applied to aligned bar series (including no lookahead)."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest

from app.services import analytics
from app.services.prices import PriceBarData
from tests import finance_helpers as fh

PARAMS = analytics.AnalyticsParams(
    estimation_sessions=120,
    min_sessions=60,
    winsor_z=5.0,
    rvol_lookback_sessions=20,
    drift_label_fraction=0.5,
    regime_window_sessions=20,
    regime_lookback_sessions=252,
)


def bar(day: date, close: float, *, open_: float | None = None, adj: float | None = None, volume: int = 100) -> PriceBarData:
    return PriceBarData(
        date=day, open=open_ if open_ is not None else close, high=None, low=None,
        close=close, adj_close=adj if adj is not None else close, volume=volume,
    )


def test_the_gap_and_intraday_legs_multiply_to_the_return():
    # A dividend-adjusted series: adj_close is 98% of close on both days.
    previous = bar(date(2026, 8, 26), 100.0, adj=98.0)
    today = bar(date(2026, 8, 27), 108.0, open_=104.0, adj=105.84)
    gap, intraday = analytics.legs(previous, today)

    daily_return = 105.84 / 98.0 - 1
    assert (1 + gap) * (1 + intraday) == pytest.approx(1 + daily_return, rel=1e-12)
    assert gap == pytest.approx(104.0 * (105.84 / 108.0) / 98.0 - 1)
    assert intraday == pytest.approx(108.0 / 104.0 - 1)


def test_legs_need_an_open_and_a_previous_bar():
    today = bar(date(2026, 8, 27), 108.0)
    assert analytics.legs(None, today) == (None, None)
    assert analytics.legs(bar(date(2026, 8, 26), 100.0), replace(today, open=None)) == (None, None)


def test_relative_volume_is_against_the_median_of_the_prior_sessions():
    start = date(2026, 1, 1)
    prior = [bar(start + timedelta(days=i), 10.0, volume=100 + (i % 3) * 50) for i in range(20)]
    today = bar(start + timedelta(days=20), 10.0, volume=600)

    assert analytics.relative_volume(prior, today, 20) == pytest.approx(600 / 150)
    assert analytics.relative_volume(prior[1:], today, 20) is None  # too little history


@pytest.mark.parametrize(
    ("car", "abnormal", "label"),
    [
        (0.03, 0.05, "continued"),  # same sign, at least half
        (-0.03, 0.05, "reversed"),
        (0.01, 0.05, "held"),  # under half
        (-0.024, -0.05, "held"),
        (-0.025, -0.05, "continued"),  # exactly half counts
        (None, 0.05, None),  # the sessions do not exist yet
    ],
)
def test_drift_labels(car, abnormal, label):
    assert analytics.drift_label(car, abnormal, 0.5) == label


def test_the_volatility_regime_is_annualised_and_ranked():
    calm = [0.005 * (-1) ** i for i in range(200)]
    stormy = [0.03 * (-1) ** i for i in range(20)]
    vol, percentile = analytics.volatility_regime(calm + stormy, 20, 252)

    assert vol == pytest.approx(float(np.std(stormy, ddof=1)) * math.sqrt(252))
    assert percentile == 100.0  # the highest of the past year
    assert analytics.volatility_regime(calm[:19], 20, 252) == (None, None)
    vol_only, no_rank = analytics.volatility_regime(calm[:30], 20, 252)
    assert vol_only is not None and no_rank is None  # too short to rank


def test_a_move_is_decomposed_against_its_benchmarks():
    data = fh.factor_histories(200, shocks={150: (0.0, -0.05, 0.0)})  # a sector-only day
    aligned = analytics.align_returns(data.stock.bars, data.market.bars, data.sector.bars)
    day = data.dates[150]

    result = analytics.analyse(day, data.stock.bars, aligned, PARAMS)
    d = result.decomposition

    assert d is not None and result.decomposition_warning is None
    assert d.dominant == "sector"
    assert d.model.sessions == 120
    assert d.model.alpha + d.market + d.sector + d.abnormal == pytest.approx(d.total, abs=1e-15)
    stock_return = data.stock.bars[150].adj_close / data.stock.bars[149].adj_close - 1
    assert d.total == pytest.approx(stock_return, rel=1e-12)


def test_the_estimation_window_ends_the_session_before_the_move():
    """No lookahead: the model at T sees nothing from T or after."""
    data = fh.factor_histories(200)
    day = data.dates[150]

    def model_at(stock_bars):
        aligned = analytics.align_returns(stock_bars, data.market.bars, data.sector.bars)
        d = analytics.analyse(day, stock_bars, aligned, PARAMS).decomposition
        assert d is not None
        return d

    baseline = model_at(data.stock.bars)
    # Change the move itself and everything after it.
    altered = [
        replace(b, adj_close=b.adj_close * (1.3 if k >= 150 else 1.0), close=b.close * (1.3 if k >= 150 else 1.0))
        for k, b in enumerate(data.stock.bars)
    ]
    moved = model_at(altered)

    assert moved.model == baseline.model
    assert moved.abnormal == pytest.approx(baseline.abnormal + 0.3 * (1 + baseline.total), rel=1e-9)


def test_drift_after_the_move_uses_the_model_fixed_at_the_move():
    shocks = {150: (0.0, 0.0, -0.08)} | {150 + k: (0.0, 0.0, 0.01) for k in range(1, 21)}
    data = fh.factor_histories(200, shocks=shocks)
    aligned = analytics.align_returns(data.stock.bars, data.market.bars, data.sector.bars)
    result = analytics.analyse(data.dates[150], data.stock.bars, aligned, PARAMS)

    d = result.decomposition
    assert d is not None
    expected_5 = sum(
        d.model.abnormal(float(aligned.stock[j]), float(aligned.market[j]), float(aligned.sector[j]))
        for j in range(aligned.index[data.dates[150]] + 1, aligned.index[data.dates[150]] + 6)
    )
    assert result.car_short == pytest.approx(expected_5)
    assert result.drift_long == "reversed"  # +20% of own return against a -8% day

    near_the_end = analytics.analyse(data.dates[195], data.stock.bars, aligned, PARAMS)
    assert near_the_end.car_short is None and near_the_end.drift_short is None


def test_a_missing_benchmark_bar_on_the_move_falls_back_with_a_reason():
    data = fh.factor_histories(200)
    day = data.dates[150]
    market = [b for b in data.market.bars if b.date != data.dates[149]]  # the session before
    aligned = analytics.align_returns(data.stock.bars, market, data.sector.bars)

    result = analytics.analyse(day, data.stock.bars, aligned, PARAMS)
    assert result.decomposition is None
    assert result.decomposition_warning == analytics.WARNING_MISSING_BENCHMARK
    assert result.gap is not None  # the stock's own analytics do not need benchmarks


def test_too_little_history_falls_back_with_a_reason():
    data = fh.factor_histories(200)
    aligned = analytics.align_returns(data.stock.bars, data.market.bars, data.sector.bars)
    result = analytics.analyse(data.dates[30], data.stock.bars, aligned, PARAMS)

    assert result.decomposition is None
    assert "29 sessions of history" in (result.decomposition_warning or "")


def test_no_decomposition_at_all_says_why():
    data = fh.factor_histories(200)
    result = analytics.analyse(data.dates[150], data.stock.bars, None, PARAMS, unavailable_reason="a benchmark")

    assert result.decomposition is None and result.decomposition_warning == "a benchmark"
    assert result.car_short is None and result.drift_short is None
    assert result.relative_volume is not None and result.realized_vol is not None
