"""Detection modes: which days are movements, and what flagged them.

`raw` is the v1 detector, unchanged (`app.services.movements`): ``|r| >=
max(floor, k * rolling_std)`` on the stock alone. `abnormal` flags a day whose
standardised abnormal return clears ``MOVEMENT_ABNORMAL_K`` and whose return
clears the floor, so a 4% fall the sector explains does not stand out but a
4% fall on a flat day does. `union` is either, so nothing raw flags ever
disappears.

Which return the floor applies to is a setting
(``MOVEMENT_ABNORMAL_FLOOR_BASIS``). The brief specifies the day's return,
``|r| >= floor``; but then a stock that was flat on a day its sector fell 4%,
the brief's own example of what abnormal detection should reveal, can never
qualify, since its ``|r|`` is below the floor. With basis ``abnormal`` the
floor applies to ``|abnormal return|`` instead, which does flag it and still
keeps tiny residuals on very quiet stocks out.

Every evaluated day carries a `trigger`: `raw`, `abnormal` or `both`, whatever
the mode, so the audit trail says which test a movement passed. A day that
cannot be decomposed (too little history, missing benchmark bars, or the
ticker is itself a benchmark) falls back to the raw test alone, with trigger
`raw`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.services.movements import DailyReturn

DetectionMode = Literal["raw", "abnormal", "union"]
Trigger = Literal["raw", "abnormal", "both"]
FloorBasis = Literal["return", "abnormal"]

TRIGGER_RAW: Trigger = "raw"
TRIGGER_ABNORMAL: Trigger = "abnormal"
TRIGGER_BOTH: Trigger = "both"


@dataclass(frozen=True, slots=True)
class AbnormalReading:
    """What the decomposition says about one day."""

    sar: float | None
    abnormal: float


@dataclass(frozen=True, slots=True)
class Verdict:
    day: DailyReturn
    raw: bool
    # None when the day could not be decomposed, so the abnormal test was not run.
    abnormal: bool | None
    qualifies: bool
    trigger: Trigger | None


def abnormal_test(
    daily_return: float,
    reading: AbnormalReading | None,
    *,
    abnormal_k: float,
    floor: float,
    floor_basis: FloorBasis = "return",
) -> bool | None:
    """None when the day was not decomposed (or had no SAR)."""
    if reading is None or reading.sar is None:
        return None
    sized = daily_return if floor_basis == "return" else reading.abnormal
    return abs(reading.sar) >= abnormal_k and abs(sized) >= floor and abs(sized) > 0


def trigger_for(raw: bool, abnormal: bool | None) -> Trigger | None:
    if raw and abnormal:
        return TRIGGER_BOTH
    if raw:
        return TRIGGER_RAW
    if abnormal:
        return TRIGGER_ABNORMAL
    return None


def qualifies(mode: DetectionMode, raw: bool, abnormal: bool | None) -> bool:
    if abnormal is None:  # not decomposable: the raw test alone, whatever the mode
        return raw
    if mode == "raw":
        return raw
    if mode == "abnormal":
        return abnormal
    return raw or abnormal


def evaluate(
    returns: Iterable[DailyReturn],
    readings: Mapping[date, AbnormalReading | None],
    *,
    mode: DetectionMode,
    abnormal_k: float,
    floor: float,
    floor_basis: FloorBasis = "return",
) -> list[Verdict]:
    """A verdict for every day; `readings` holds each decomposed day's SAR."""
    verdicts = []
    for day in returns:
        abnormal = abnormal_test(
            day.daily_return,
            readings.get(day.date),
            abnormal_k=abnormal_k,
            floor=floor,
            floor_basis=floor_basis,
        )
        verdicts.append(
            Verdict(
                day=day,
                raw=day.is_major,
                abnormal=abnormal,
                qualifies=qualifies(mode, day.is_major, abnormal),
                trigger=trigger_for(day.is_major, abnormal),
            )
        )
    return verdicts
