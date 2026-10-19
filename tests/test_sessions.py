"""Session-aware timing on the real XNYS calendar.

The cases the brief names: an early close (the day after Thanksgiving), a
holiday Monday, both daylight-saving changes, and a Friday move whose next
session is Monday. Instants are written in UTC with the New York time beside
them, since the point is that the code, not the reader, does the conversion.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.config import settings
from app.services import sessions
from app.services.news.queries import WINDOW_SESSIONS, news_window_closes_at, search_window
from app.services.sessions import (
    AFTER_CLOSE,
    BEFORE_PRIOR_CLOSE,
    INTRADAY,
    LATER,
    OVERNIGHT,
    PRECISION_DAY,
    PRECISION_TIME,
)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def timing(published: datetime | None, move: date) -> tuple[str | None, str | None]:
    result = sessions.classify(published, sessions.frame_for(move))
    return result.timing, result.precision


def test_the_day_after_thanksgiving_closes_at_one_in_the_afternoon():
    move = date(2025, 11, 28)
    frame = sessions.frame_for(move)
    assert frame is not None
    assert frame.close == utc(2025, 11, 28, 18)  # 13:00 EST
    assert frame.previous == date(2025, 11, 26)  # Thanksgiving is closed

    assert timing(utc(2025, 11, 28, 17, 59), move) == (INTRADAY, PRECISION_TIME)  # 12:59
    assert timing(utc(2025, 11, 28, 18, 30), move) == (AFTER_CLOSE, PRECISION_TIME)  # 13:30, not intraday
    assert timing(utc(2025, 11, 27, 15), move) == (OVERNIGHT, PRECISION_TIME)  # Thanksgiving Day


def test_a_holiday_monday_is_overnight_for_the_tuesday_move():
    move = date(2025, 1, 21)  # Tuesday after Martin Luther King Jr. Day
    frame = sessions.frame_for(move)
    assert frame is not None and frame.previous == date(2025, 1, 17)  # Friday

    assert timing(utc(2025, 1, 20, 15), move) == (OVERNIGHT, PRECISION_TIME)  # Monday 10:00
    assert timing(utc(2025, 1, 20), move) == (OVERNIGHT, PRECISION_DAY)  # the whole holiday
    assert timing(utc(2025, 1, 18), move) == (OVERNIGHT, PRECISION_DAY)  # Saturday
    assert sessions.frame_for(date(2025, 1, 20)) is None  # no session that day


def test_the_spring_daylight_saving_change():
    move = date(2025, 3, 10)  # Monday; clocks went forward on Sunday
    frame = sessions.frame_for(move)
    assert frame is not None
    assert frame.prior_close == utc(2025, 3, 7, 21)  # Friday 16:00 EST
    assert frame.open == utc(2025, 3, 10, 13, 30)  # Monday 09:30 EDT

    assert timing(utc(2025, 3, 10, 13), move) == (OVERNIGHT, PRECISION_TIME)  # 09:00 EDT
    # 13:45 UTC is 09:45 EDT: open. On last week's offset it would be 08:45.
    assert timing(utc(2025, 3, 10, 13, 45), move) == (INTRADAY, PRECISION_TIME)


def test_the_autumn_daylight_saving_change():
    move = date(2025, 11, 3)  # Monday; clocks went back on Sunday
    frame = sessions.frame_for(move)
    assert frame is not None
    assert frame.prior_close == utc(2025, 10, 31, 20)  # Friday 16:00 EDT
    assert frame.open == utc(2025, 11, 3, 14, 30)  # Monday 09:30 EST

    assert timing(utc(2025, 10, 31, 20, 30), move) == (OVERNIGHT, PRECISION_TIME)  # 16:30 EDT
    assert timing(utc(2025, 11, 3, 14), move) == (OVERNIGHT, PRECISION_TIME)  # 09:00 EST
    assert timing(utc(2025, 11, 3, 14, 30), move) == (INTRADAY, PRECISION_TIME)  # the open


def test_a_friday_move_runs_to_mondays_open():
    move = date(2025, 3, 7)
    frame = sessions.frame_for(move)
    assert frame is not None and frame.next == date(2025, 3, 10)

    assert timing(utc(2025, 3, 8, 12), move) == (AFTER_CLOSE, PRECISION_TIME)  # Saturday
    assert timing(utc(2025, 3, 10, 13), move) == (AFTER_CLOSE, PRECISION_TIME)  # Monday 09:00
    assert timing(utc(2025, 3, 10, 13, 30), move) == (LATER, PRECISION_TIME)  # Monday open
    assert timing(utc(2025, 3, 9), move) == (AFTER_CLOSE, PRECISION_DAY)  # Sunday, date only
    assert timing(utc(2025, 3, 10), move) == (None, PRECISION_DAY)  # Monday: before or after the open
    assert timing(utc(2025, 3, 11), move) == (LATER, PRECISION_DAY)


def test_news_after_the_prior_close_is_overnight_not_the_day_before():
    """The brief's example: 17:04 New York time the evening before the move."""
    move = date(2025, 3, 6)  # Thursday
    assert timing(utc(2025, 3, 5, 22, 4), move) == (OVERNIGHT, PRECISION_TIME)  # Wed 17:04 EST
    assert timing(utc(2025, 3, 5, 20, 59), move) == (BEFORE_PRIOR_CLOSE, PRECISION_TIME)  # 15:59


def test_boundaries_are_inclusive_of_the_closes():
    move = date(2025, 3, 6)
    frame = sessions.frame_for(move)
    assert frame is not None
    assert timing(frame.prior_close, move) == (BEFORE_PRIOR_CLOSE, PRECISION_TIME)
    assert timing(frame.close, move) == (INTRADAY, PRECISION_TIME)
    assert timing(frame.next_open, move) == (LATER, PRECISION_TIME)


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        (utc(2025, 3, 3), BEFORE_PRIOR_CLOSE),  # before P's date
        (utc(2025, 3, 5), None),  # P's date: before or after its close
        (utc(2025, 3, 6), None),  # T's date: overnight, intraday or after
        (utc(2025, 3, 7), None),  # N's date: after close or later
        (utc(2025, 3, 10), LATER),
    ],
)
def test_a_date_only_article_gets_a_bucket_only_when_its_whole_day_fits(published, expected):
    assert timing(published, date(2025, 3, 6)) == (expected, PRECISION_DAY)


def test_unknown_dates_and_non_sessions_have_no_timing():
    assert timing(None, date(2025, 3, 6)) == (None, None)
    assert timing(utc(2025, 3, 6, 15), date(2025, 3, 8)) == (None, None)  # a Saturday "move"


def test_a_naive_timestamp_is_read_as_utc():
    naive = datetime(2025, 3, 5, 22, 4)
    assert timing(naive, date(2025, 3, 6)) == (OVERNIGHT, PRECISION_TIME)


def test_the_session_window_spans_sessions_not_calendar_days(monkeypatch):
    monkeypatch.setattr(settings, "news_window_sessions_before", 2)
    monkeypatch.setattr(settings, "news_window_sessions_after", 1)
    monkeypatch.setattr(settings, "news_window_days_before", 3)
    monkeypatch.setattr(settings, "news_window_days_after", 1)
    friday = date(2025, 3, 7)

    start, end = search_window(friday, WINDOW_SESSIONS)
    assert start == utc(2025, 3, 4, 21)  # Tuesday's close: covers Wed, Thu and their evenings
    assert end == utc(2025, 3, 11, 13, 30)  # Tuesday's open: covers Monday and its after-hours

    calendar_start, calendar_end = search_window(friday)
    assert calendar_end.date() == date(2025, 3, 8)  # the v1 window stops on Saturday
    assert calendar_start == utc(2025, 3, 4)


def test_the_session_window_falls_back_to_calendar_days_off_session(monkeypatch):
    saturday = date(2025, 3, 8)
    assert search_window(saturday, WINDOW_SESSIONS) == search_window(saturday)


def test_window_close_follows_the_mode(monkeypatch):
    monkeypatch.setattr(settings, "news_window_grace_hours", 6)
    friday = date(2025, 3, 7)
    assert news_window_closes_at(friday, WINDOW_SESSIONS) > news_window_closes_at(friday)
