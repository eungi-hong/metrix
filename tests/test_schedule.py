"""Tests for the nightly schedule: weekdays only, 17:15 New York time, across
both daylight-saving changes and over weekends."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.config import settings
from app.services.schedule import latest_run_at, next_run_after, trading_date


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def new_york_1715(monkeypatch):
    monkeypatch.setattr(settings, "prewarm_run_at", "17:15")
    monkeypatch.setattr(settings, "prewarm_timezone", "America/New_York")


@pytest.mark.parametrize(
    ("now", "expected", "why"),
    [
        # Summer (EDT, UTC-4): 17:15 local is 21:15 UTC.
        (utc(2026, 7, 7, 12, 0), utc(2026, 7, 7, 21, 15), "Tuesday morning: tonight"),
        (utc(2026, 7, 7, 22, 0), utc(2026, 7, 8, 21, 15), "Tuesday evening: Wednesday"),
        (utc(2026, 7, 7, 21, 15), utc(2026, 7, 8, 21, 15), "exactly on time: the next one"),
        (utc(2026, 7, 10, 22, 0), utc(2026, 7, 13, 21, 15), "Friday evening: Monday"),
        (utc(2026, 7, 11, 15, 0), utc(2026, 7, 13, 21, 15), "Saturday: Monday"),
        (utc(2026, 7, 12, 23, 0), utc(2026, 7, 13, 21, 15), "Sunday night: Monday"),
        # Winter (EST, UTC-5): 17:15 local is 22:15 UTC.
        (utc(2026, 1, 14, 12, 0), utc(2026, 1, 14, 22, 15), "January: an hour later in UTC"),
        # Late evening New York is already tomorrow in UTC.
        (utc(2026, 1, 15, 2, 0), utc(2026, 1, 15, 22, 15), "21:00 EST Wednesday: Thursday"),
    ],
)
def test_next_run_after(now, expected, why):
    assert next_run_after(now) == expected, why


def test_spring_forward_over_the_weekend():
    """Clocks go forward on Sunday 2026-03-08. Friday's run is 22:15 UTC
    (EST); Monday's is 21:15 UTC (EDT): the same 17:15 on the wall clock."""
    friday_evening = utc(2026, 3, 6, 23, 0)

    monday = next_run_after(friday_evening)

    assert monday == utc(2026, 3, 9, 21, 15)
    assert latest_run_at(friday_evening) == utc(2026, 3, 6, 22, 15)


def test_fall_back_over_the_weekend():
    """Clocks go back on Sunday 2026-11-01: 21:15 UTC before, 22:15 UTC after."""
    friday_evening = utc(2026, 10, 30, 22, 0)

    assert latest_run_at(friday_evening) == utc(2026, 10, 30, 21, 15)
    assert next_run_after(friday_evening) == utc(2026, 11, 2, 22, 15)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (utc(2026, 7, 7, 22, 0), utc(2026, 7, 7, 21, 15)),  # tonight's, already past
        (utc(2026, 7, 7, 12, 0), utc(2026, 7, 6, 21, 15)),  # before tonight's: Monday's
        (utc(2026, 7, 13, 12, 0), utc(2026, 7, 10, 21, 15)),  # Monday morning: Friday's
        (utc(2026, 7, 7, 21, 15), utc(2026, 7, 7, 21, 15)),  # exactly on time: this one
    ],
)
def test_latest_run_at(now, expected):
    assert latest_run_at(now) == expected


def test_runs_belong_to_their_new_york_date():
    assert trading_date(utc(2026, 7, 7, 21, 15)) == date(2026, 7, 7)
    # 22:15 UTC on 2026-01-14 is still the 14th in New York.
    assert trading_date(utc(2026, 1, 14, 22, 15)) == date(2026, 1, 14)
    # 02:00 UTC on the 15th is the evening of the 14th in New York.
    assert trading_date(utc(2026, 1, 15, 2, 0)) == date(2026, 1, 14)


def test_the_run_time_and_zone_are_configurable(monkeypatch):
    monkeypatch.setattr(settings, "prewarm_run_at", "18:30")
    monkeypatch.setattr(settings, "prewarm_timezone", "Europe/London")

    # British Summer Time (UTC+1): 18:30 local is 17:30 UTC.
    assert next_run_after(utc(2026, 7, 7, 9, 0)) == utc(2026, 7, 7, 17, 30)
