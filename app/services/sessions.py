"""Trading sessions, and when an article was published relative to a move.

A daily move is close-to-close: from the prior session's close to this
session's close. Calendar days do not describe that. A Friday move's "T+1" is
a Saturday, and an article at 17:04 New York time the evening before is
"T-1" by date but is really the classic catalyst: news after the prior close
that the next open prices in. So timing here is in sessions of the New York
Stock Exchange (`exchange_calendars`, calendar XNYS), which knows holidays,
early closes and both daylight-saving changes. Instants are UTC throughout;
New York time appears only where a bare date has to be read as a local day.

For a movement on session T, with previous session P and next session N,
every article falls in one bucket:

    before_prior_close   at or before close(P): mostly priced in at P
    overnight            after close(P), before open(T): explains the gap
    intraday             open(T) to close(T): explains the intraday leg
    after_close          after close(T), before open(N): cannot have caused
                         T's close-to-close move (a recap, or a cause of N)
    later                from open(N): cannot have caused it

An article with only a date (published exactly at midnight UTC, which is how
date-only timestamps arrive) gets `timing_precision = "day"` and is placed in
a bucket only when the whole of that New York day lies inside it. A date on P,
T or N could fall in two buckets, so it gets none rather than a guess.

This module is pure apart from the calendar, which is built once per process.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from functools import lru_cache
from typing import Literal

import exchange_calendars as xcals
import pandas as pd

from app.core.logging import get_logger

logger = get_logger(__name__)

EXCHANGE = "XNYS"
# The calendar is built from this date to a year past today; older movements
# fall back to calendar-day handling.
CALENDAR_START = "2000-01-01"

Timing = Literal["before_prior_close", "overnight", "intraday", "after_close", "later"]
TimingPrecision = Literal["time", "day"]

BEFORE_PRIOR_CLOSE: Timing = "before_prior_close"
OVERNIGHT: Timing = "overnight"
INTRADAY: Timing = "intraday"
AFTER_CLOSE: Timing = "after_close"
LATER: Timing = "later"
TIMINGS: tuple[Timing, ...] = (BEFORE_PRIOR_CLOSE, OVERNIGHT, INTRADAY, AFTER_CLOSE, LATER)

PRECISION_TIME: TimingPrecision = "time"
PRECISION_DAY: TimingPrecision = "day"

# Buckets that can explain a close-to-close move on T. The others cannot.
CAUSAL_TIMINGS: frozenset[Timing] = frozenset({BEFORE_PRIOR_CLOSE, OVERNIGHT, INTRADAY})


@lru_cache(maxsize=1)
def calendar() -> xcals.ExchangeCalendar:
    """The XNYS calendar, built once (it takes most of a second)."""
    return xcals.get_calendar(EXCHANGE, start=CALENDAR_START)


@dataclass(frozen=True, slots=True)
class SessionFrame:
    """The sessions around one movement, as UTC instants."""

    session: date  # T, the movement's session
    previous: date  # P
    next: date  # N
    prior_close: datetime  # close(P)
    open: datetime  # open(T)
    close: datetime  # close(T)
    next_open: datetime  # open(N)


@dataclass(frozen=True, slots=True)
class ArticleTiming:
    """Where an article falls relative to a movement. Both None without a date."""

    timing: Timing | None
    precision: TimingPrecision | None


NO_TIMING = ArticleTiming(None, None)


def _utc(value: pd.Timestamp) -> datetime:
    return value.to_pydatetime().astimezone(timezone.utc)


def is_session(day: date) -> bool:
    cal = calendar()
    stamp = pd.Timestamp(day)
    if stamp < cal.first_session or stamp > cal.last_session:
        return False
    return bool(cal.is_session(stamp))


@lru_cache(maxsize=4096)
def frame_for(day: date) -> SessionFrame | None:
    """The session frame of a movement on `day`, or None if `day` is not an
    XNYS session the calendar covers (or has no previous or next session)."""
    if not is_session(day):
        return None
    cal = calendar()
    stamp = pd.Timestamp(day)
    if stamp <= cal.first_session or stamp >= cal.last_session:
        return None
    previous = cal.previous_session(stamp)
    following = cal.next_session(stamp)
    return SessionFrame(
        session=day,
        previous=previous.date(),
        next=following.date(),
        prior_close=_utc(cal.session_close(previous)),
        open=_utc(cal.session_open(stamp)),
        close=_utc(cal.session_close(stamp)),
        next_open=_utc(cal.session_open(following)),
    )


def has_time_of_day(published_at: datetime) -> bool:
    """False for a date-only timestamp, which arrives as exactly midnight UTC."""
    utc = _as_utc(published_at)
    return utc.time() != time(0, 0)


def classify(published_at: datetime | None, frame: SessionFrame | None) -> ArticleTiming:
    """The bucket an article published at `published_at` falls in."""
    if published_at is None or frame is None:
        return NO_TIMING
    if has_time_of_day(published_at):
        return ArticleTiming(_bucket_for_instant(_as_utc(published_at), frame), PRECISION_TIME)
    return ArticleTiming(_bucket_for_day(_as_utc(published_at).date(), frame), PRECISION_DAY)


def _bucket_for_instant(at: datetime, frame: SessionFrame) -> Timing:
    if at <= frame.prior_close:
        return BEFORE_PRIOR_CLOSE
    if at < frame.open:
        return OVERNIGHT
    if at <= frame.close:
        return INTRADAY
    if at < frame.next_open:
        return AFTER_CLOSE
    return LATER


def _bucket_for_day(day: date, frame: SessionFrame) -> Timing | None:
    """A bucket only when the whole New York day `day` lies inside it.

    Sessions open and close on their own date, so a day strictly between two
    session dates is entirely after the first close and before the next open.
    """
    if day < frame.previous:
        return BEFORE_PRIOR_CLOSE
    if frame.previous < day < frame.session:
        return OVERNIGHT
    if frame.session < day < frame.next:
        return AFTER_CLOSE
    if day > frame.next:
        return LATER
    return None  # on P, T or N: either side of a session boundary


def offset_session(day: date, sessions: int) -> date | None:
    """The session `sessions` sessions after (or before, if negative) `day`,
    which must itself be a session. None outside the calendar."""
    if not is_session(day):
        return None
    cal = calendar()
    try:
        return cal.session_offset(pd.Timestamp(day), sessions).date()
    except ValueError as exc:  # RequestedSessionOutOfBounds: beyond the calendar
        logger.info("session_offset_out_of_range", day=day.isoformat(), sessions=sessions, error=str(exc))
        return None


def session_window(day: date, before: int, after: int) -> tuple[datetime, datetime] | None:
    """The news window for a movement on session `day`, in sessions.

    It opens at the close of the session `before + 1` sessions earlier, so
    the `before` sessions ahead of the move are covered from the evening
    before the first of them; and it ends at the open of the session `after
    + 1` sessions later, so the `after` sessions behind the move are covered
    through their own after-hours. None if `day` is not a session.
    """
    start_session = offset_session(day, -(before + 1))
    end_session = offset_session(day, after + 1)
    if start_session is None or end_session is None:
        return None
    cal = calendar()
    return (
        _utc(cal.session_close(pd.Timestamp(start_session))),
        _utc(cal.session_open(pd.Timestamp(end_session))),
    )


def _as_utc(value: datetime) -> datetime:
    """Naive datetimes (SQLite) are UTC by convention in this codebase."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
