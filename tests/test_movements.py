"""Unit tests for the volatility-adjusted movement detector.

This is the one genuinely load-bearing piece of pure logic in the system, so
it gets real coverage: the floor, the sigma term, the interaction between
them, the no-lookahead guarantee, and the degenerate inputs.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta

import pytest

from app.models.enums import Direction
from app.services.movements import (
    SOURCE_FLOOR,
    SOURCE_VOLATILITY,
    DetectionParams,
    PricePoint,
    compute_returns,
    detect_movements,
)

START = date(2024, 1, 1)


def series(returns: list[float], start_price: float = 100.0) -> list[PricePoint]:
    """Build a price series that realises exactly `returns`, day by day."""
    points = [PricePoint(START, start_price)]
    price = start_price
    for i, r in enumerate(returns, start=1):
        price *= 1 + r
        points.append(PricePoint(START + timedelta(days=i), price))
    return points


def flat_then(shock: float, *, quiet_days: int = 40, quiet_return: float = 0.0):
    return series([quiet_return] * quiet_days + [shock])


# --------------------------------------------------------------- basic math


def test_daily_return_is_computed_from_adjusted_close():
    points = [PricePoint(START, 100.0), PricePoint(START + timedelta(days=1), 105.0)]
    (result,) = compute_returns(points)
    assert result.daily_return == pytest.approx(0.05)
    assert result.prev_adj_close == 100.0
    assert result.adj_close == 105.0


def test_first_bar_produces_no_return():
    points = [PricePoint(START, 100.0)]
    assert compute_returns(points) == []


def test_returns_are_in_date_order_even_if_input_is_not():
    points = [
        PricePoint(START + timedelta(days=2), 102.0),
        PricePoint(START, 100.0),
        PricePoint(START + timedelta(days=1), 101.0),
    ]
    results = compute_returns(points)
    assert [r.date for r in results] == [START + timedelta(days=1), START + timedelta(days=2)]


def test_duplicate_dates_collapse_to_one_bar():
    points = [
        PricePoint(START, 100.0),
        PricePoint(START + timedelta(days=1), 101.0),
        PricePoint(START + timedelta(days=1), 110.0),  # corrected bar, wins
    ]
    (result,) = compute_returns(points)
    assert result.adj_close == 110.0


@pytest.mark.parametrize("bad", [0.0, -5.0, None])
def test_non_positive_prices_are_discarded(bad):
    points = [
        PricePoint(START, 100.0),
        PricePoint(START + timedelta(days=1), bad),
        PricePoint(START + timedelta(days=2), 103.0),
    ]
    results = compute_returns(points)
    # The bad bar is dropped and the return bridges the gap; no ZeroDivisionError.
    assert len(results) == 1
    assert results[0].daily_return == pytest.approx(0.03)


# ------------------------------------------------------------------ the floor


def test_floor_applies_before_the_window_fills():
    """With no volatility history yet, the flat floor is the only threshold."""
    points = series([0.03])  # day 1: +3%, zero prior returns
    (result,) = compute_returns(points, DetectionParams(window=20, k=2.0, floor=0.02))
    assert result.rolling_std is None
    assert result.threshold == pytest.approx(0.02)
    assert result.threshold_source == SOURCE_FLOOR
    assert result.is_major is True


def test_move_below_the_floor_is_not_major_in_a_quiet_stock():
    """1% on a stock that never moves is noise, not news -- the floor blocks it."""
    points = flat_then(0.01)
    assert detect_movements(points, DetectionParams(floor=0.02, k=2.0)) == []


def test_low_volatility_stock_uses_the_floor_not_two_sigma():
    points = flat_then(0.025)
    (movement,) = detect_movements(points, DetectionParams(floor=0.02, k=2.0))
    assert movement.threshold_source == SOURCE_FLOOR
    assert movement.threshold == pytest.approx(0.02)


# ------------------------------------------------------- the volatility term


def test_volatility_term_binds_for_a_jumpy_stock():
    """A 3% day is unremarkable for a stock that alternates +/-4%."""
    noisy = [0.04 if i % 2 else -0.04 for i in range(40)]
    points = series([*noisy, 0.03])
    results = compute_returns(points, DetectionParams(window=20, k=2.0, floor=0.02))
    last = results[-1]
    assert last.threshold_source == SOURCE_VOLATILITY
    assert last.threshold > 0.02
    assert last.is_major is False


def test_volatility_term_still_flags_a_big_enough_move_in_a_jumpy_stock():
    noisy = [0.04 if i % 2 else -0.04 for i in range(40)]
    points = series([*noisy, 0.25])
    last = compute_returns(points, DetectionParams(window=20, k=2.0, floor=0.02))[-1]
    assert last.threshold_source == SOURCE_VOLATILITY
    assert last.is_major is True


def test_threshold_equals_k_times_rolling_std_of_the_prior_window():
    """Verify the threshold against an independently computed stdev."""
    params = DetectionParams(window=5, k=2.0, floor=0.0)
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.05]
    results = compute_returns(series(returns), params)

    final = results[-1]
    expected_std = statistics.stdev(returns[-6:-1])  # the 5 days before the last
    assert final.rolling_std == pytest.approx(expected_std)
    assert final.threshold == pytest.approx(2.0 * expected_std)
    assert final.sigma_multiple == pytest.approx(abs(returns[-1]) / expected_std)


def test_k_is_configurable_and_changes_the_verdict():
    noisy = [0.01 if i % 2 else -0.01 for i in range(30)]
    points = series([*noisy, 0.03])

    lenient = compute_returns(points, DetectionParams(window=20, k=2.0, floor=0.001))[-1]
    strict = compute_returns(points, DetectionParams(window=20, k=4.0, floor=0.001))[-1]

    assert lenient.is_major is True
    assert strict.is_major is False
    assert strict.threshold > lenient.threshold


def test_window_is_configurable():
    params_short = DetectionParams(window=3, k=2.0, floor=0.02)
    params_long = DetectionParams(window=30, k=2.0, floor=0.02)
    points = series([0.001] * 10)

    short = compute_returns(points, params_short)[-1]
    long = compute_returns(points, params_long)[-1]

    assert short.rolling_std is not None  # 3-day window has filled
    assert long.rolling_std is None  # 30-day window has not


# -------------------------------------------------------------- no lookahead


def test_threshold_excludes_the_day_being_tested():
    """The shock day must not inflate its own threshold.

    If the rolling window included the current day, a single huge move would
    raise sigma enough to hide itself. Here the threshold on the shock day must
    equal the threshold computed from the quiet days alone.
    """
    params = DetectionParams(window=10, k=2.0, floor=0.0)
    quiet = [0.005, -0.005] * 10
    with_shock = compute_returns(series([*quiet, 0.30]), params)[-1]
    without_shock = compute_returns(series([*quiet, 0.001]), params)[-1]

    assert with_shock.rolling_std == pytest.approx(without_shock.rolling_std)
    assert with_shock.is_major is True


def test_threshold_is_computable_from_information_available_at_the_prior_close():
    """Truncating the series after day N must not change day N's threshold."""
    params = DetectionParams(window=5, k=2.0, floor=0.0)
    returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.05, -0.07, 0.02]
    full = compute_returns(series(returns), params)

    for cut in range(6, len(returns) + 1):
        truncated = compute_returns(series(returns[:cut]), params)
        assert truncated[-1].threshold == pytest.approx(full[cut - 1].threshold)


# --------------------------------------------------------------- direction


def test_direction_and_magnitude():
    up = compute_returns(series([0.05]))[0]
    down = compute_returns(series([-0.05]))[0]

    assert up.direction is Direction.UP
    assert down.direction is Direction.DOWN
    assert up.abs_return == pytest.approx(down.abs_return)


def test_detection_is_symmetric_in_direction():
    """A -4% day and a +4% day face the same threshold."""
    params = DetectionParams(window=10, k=2.0, floor=0.02)
    quiet = [0.001] * 15
    up = compute_returns(series([*quiet, 0.04]), params)[-1]
    down = compute_returns(series([*quiet, -0.04]), params)[-1]

    assert up.threshold == pytest.approx(down.threshold)
    assert up.is_major and down.is_major


# ------------------------------------------------------------ degenerate cases


def test_empty_and_single_point_inputs():
    assert compute_returns([]) == []
    assert detect_movements([PricePoint(START, 100.0)]) == []


def test_perfectly_flat_series_produces_no_movements():
    """Zero threshold and zero return must not count as a movement."""
    points = series([0.0] * 30)
    assert detect_movements(points, DetectionParams(window=5, k=2.0, floor=0.0)) == []


def test_zero_volatility_window_flags_any_nonzero_move_when_floor_is_zero():
    points = series([0.0] * 30 + [0.001])
    (movement,) = detect_movements(points, DetectionParams(window=5, k=2.0, floor=0.0))
    assert movement.daily_return == pytest.approx(0.001)


def test_move_exactly_at_the_threshold_is_major():
    """The definition is `>=`; the boundary counts."""
    points = series([0.02])
    (result,) = compute_returns(points, DetectionParams(floor=0.02))
    assert result.is_major is True


# ------------------------------------------------------------------- params


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": 1},
        {"window": 0},
        {"k": 0.0},
        {"k": -1.0},
        {"floor": -0.01},
    ],
)
def test_invalid_parameters_are_rejected(kwargs):
    with pytest.raises(ValueError):
        DetectionParams(**kwargs)


def test_params_are_read_from_settings():
    from app.core.config import Settings

    params = DetectionParams.from_settings(
        Settings(movement_std_window=15, movement_k=3.0, movement_floor_pct=0.05)
    )
    assert (params.window, params.k, params.floor) == (15, 3.0, 0.05)


# ---------------------------------------------- upstream error classification


@pytest.mark.parametrize(
    "message",
    [
        "$ZZZZ: possibly delisted; no timezone found",
        "ZZZZ: No data found for this date range",
        "No price data found for symbol",
    ],
)
def test_yfinance_not_found_messages_are_classified_as_unknown_symbol(message):
    """A symbol that does not exist must surface as 404, not 502.

    yfinance reports "this symbol does not exist" and "the network broke"
    through the same exception type, separable only by the message.
    """
    from app.services.prices import _looks_like_unknown_symbol

    assert _looks_like_unknown_symbol(message) is True


@pytest.mark.parametrize(
    "message",
    ["Connection reset by peer", "HTTPError: 500 Server Error", "read timed out"],
)
def test_transport_failures_are_not_mistaken_for_unknown_symbols(message):
    from app.services.prices import _looks_like_unknown_symbol

    assert _looks_like_unknown_symbol(message) is False
