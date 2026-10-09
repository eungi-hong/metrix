"""When the nightly pre-warm runs.

US equities close at 16:00 New York time. The run goes at `PREWARM_RUN_AT`
(17:15 by default), once yfinance has settled the day's bars, Monday to
Friday. Times are computed in the exchange's own time zone with `zoneinfo`,
so the run stays at 17:15 local across daylight-saving changes rather than
drifting an hour twice a year, as a fixed UTC time would.

Market holidays are not special-cased, and need not be. The run is
idempotent: on a holiday yfinance has no new bar, so detection finds no new
movements, and the run refreshes a few prices and spends nothing on news. A
holiday calendar would save those few cheap price calls at the cost of a
dependency and a list that needs maintaining every year.

These are pure functions of "now", so they are tested directly; the worker's
scheduler loop is a thin shell around them.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings

SATURDAY = 5  # date.weekday(): Monday is 0


def _zone() -> ZoneInfo:
    return ZoneInfo(settings.prewarm_timezone)


def _run_time() -> time:
    hours, minutes = settings.prewarm_run_at.split(":")
    return time(int(hours), int(minutes))


def _run_on(day: date) -> datetime:
    """The scheduled instant on `day`, local, as an aware datetime."""
    return datetime.combine(day, _run_time(), tzinfo=_zone())


def _is_run_day(day: date) -> bool:
    return day.weekday() < SATURDAY


def next_run_after(now_utc: datetime) -> datetime:
    """The first scheduled run strictly after `now_utc`, in UTC."""
    local_today = now_utc.astimezone(_zone()).date()
    day = local_today
    while True:
        candidate = _run_on(day)
        if _is_run_day(day) and candidate > now_utc:
            return candidate.astimezone(timezone.utc)
        day += timedelta(days=1)


def latest_run_at(now_utc: datetime) -> datetime:
    """The most recent scheduled run at or before `now_utc`, in UTC.

    The worker enqueues this one on every wake-up, which is what makes a
    worker that was down at 17:15 catch up when it starts again.
    """
    day = now_utc.astimezone(_zone()).date()
    while True:
        candidate = _run_on(day)
        if _is_run_day(day) and candidate <= now_utc:
            return candidate.astimezone(timezone.utc)
        day -= timedelta(days=1)


def trading_date(run_at_utc: datetime) -> date:
    """The session a run belongs to: its local calendar date.

    Also used for a manual run, whose local date may be a weekend; such a run
    simply finds nothing new, like a holiday.
    """
    return run_at_utc.astimezone(_zone()).date()
