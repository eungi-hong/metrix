"""Detection modes: raw is the v1 detector exactly; abnormal and union add to it."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.detection import AbnormalReading, abnormal_test, evaluate, qualifies, trigger_for
from app.services.movements import DetectionParams, PricePoint, compute_returns, detect_movements

PARAMS = DetectionParams(window=20, k=2.0, floor=0.02)


def series() -> list[PricePoint]:
    """Alternating +/-1.5% with a few planted moves."""
    points, price = [], 100.0
    planted = {30: -0.08, 45: 0.025, 60: 0.05}
    for i in range(80):
        price *= 1 + planted.get(i, 0.015 if i % 2 else -0.015)
        points.append(PricePoint(date=date(2024, 1, 1) + timedelta(days=i), adj_close=price))
    return points


def test_raw_mode_flags_exactly_what_the_v1_detector_flags():
    returns = compute_returns(series(), PARAMS)
    readings = {r.date: AbnormalReading(sar=9.0, abnormal=0.09) for r in returns}  # all "abnormal"

    verdicts = evaluate(returns, readings, mode="raw", abnormal_k=2.5, floor=0.02)

    flagged = [v.day.date for v in verdicts if v.qualifies]
    assert flagged == [m.date for m in detect_movements(series(), PARAMS)]


def test_union_keeps_every_raw_movement_and_adds_abnormal_ones():
    returns = compute_returns(series(), PARAMS)
    raw_days = {m.date for m in detect_movements(series(), PARAMS)}
    abnormal_day = next(r.date for r in returns if r.date not in raw_days and abs(r.daily_return) >= 0.02)
    readings = {abnormal_day: AbnormalReading(sar=-3.0, abnormal=-0.03)}

    union = {v.day.date: v for v in evaluate(returns, readings, mode="union", abnormal_k=2.5, floor=0.02)}
    flagged = {day for day, v in union.items() if v.qualifies}

    assert raw_days <= flagged
    assert flagged - raw_days == {abnormal_day}
    assert union[abnormal_day].trigger == "abnormal"


@pytest.mark.parametrize(
    ("mode", "raw", "abnormal", "expected"),
    [
        ("raw", True, False, True),
        ("raw", False, True, False),
        ("abnormal", True, False, False),
        ("abnormal", False, True, True),
        ("union", True, False, True),
        ("union", False, True, True),
        ("union", False, False, False),
        ("abnormal", True, None, True),  # not decomposable: raw decides
        ("abnormal", False, None, False),
    ],
)
def test_which_days_qualify_in_each_mode(mode, raw, abnormal, expected):
    assert qualifies(mode, raw, abnormal) is expected


@pytest.mark.parametrize(
    ("raw", "abnormal", "trigger"),
    [(True, True, "both"), (True, False, "raw"), (True, None, "raw"), (False, True, "abnormal"), (False, False, None)],
)
def test_the_trigger_names_the_tests_passed(raw, abnormal, trigger):
    assert trigger_for(raw, abnormal) == trigger


def test_the_abnormal_test_needs_both_the_sar_and_the_floor():
    reading = AbnormalReading(sar=3.1, abnormal=0.035)
    assert abnormal_test(0.025, reading, abnormal_k=2.5, floor=0.02) is True
    assert abnormal_test(0.025, AbnormalReading(sar=2.4, abnormal=0.035), abnormal_k=2.5, floor=0.02) is False
    assert abnormal_test(0.025, AbnormalReading(sar=None, abnormal=0.0), abnormal_k=2.5, floor=0.02) is None
    assert abnormal_test(0.025, None, abnormal_k=2.5, floor=0.02) is None


def test_a_flat_stock_on_a_day_its_sector_fell_needs_the_abnormal_floor_basis():
    """The brief's example. Its rule (|r| >= floor) cannot flag it; flooring
    the abnormal return instead can. The basis is a setting."""
    flat_but_abnormal = AbnormalReading(sar=4.0, abnormal=0.036)
    assert abnormal_test(0.001, flat_but_abnormal, abnormal_k=2.5, floor=0.02) is False
    assert abnormal_test(0.001, flat_but_abnormal, abnormal_k=2.5, floor=0.02, floor_basis="abnormal") is True
    tiny_residual = AbnormalReading(sar=4.0, abnormal=0.004)  # a very quiet stock
    assert abnormal_test(0.001, tiny_residual, abnormal_k=2.5, floor=0.02, floor_basis="abnormal") is False
