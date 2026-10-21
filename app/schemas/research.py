"""Response models for the research endpoints: symbol search, the movement
case file, filings, earnings, and the market pulse."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.schemas.base import ApiModel

from app.schemas.market import EvidenceState, MovementOut, TickerOut


class SymbolMatchOut(ApiModel):
    symbol: str
    name: str | None = None
    exchange: str | None = None
    is_etf: bool | None = None
    has_data: bool = Field(description="Metrix already holds researched data for it.")


class SymbolSearchOut(ApiModel):
    query: str
    results: list[SymbolMatchOut]


class ReferenceReturnOut(ApiModel):
    """A peer's or benchmark's return over the same session."""

    symbol: str | None
    name: str | None = None
    role: str = Field(description="competitor, supplier, customer, partner; or market, sector.")
    daily_return: float | None = Field(
        default=None, description="Close to close on the movement's session; null when no bars are stored."
    )


class RawTestOut(ApiModel):
    daily_return: float
    threshold: float = Field(description="max(floor, k * rolling sigma), as stored at detection.")
    threshold_source: str
    rolling_std: float | None
    k: float
    window: int
    floor: float
    passed: bool


class AbnormalTestOut(ApiModel):
    sar: float | None
    abnormal_return: float | None
    k: float = Field(description="MOVEMENT_ABNORMAL_K now (not stored per movement).")
    floor_basis: Literal["return", "abnormal"]
    floor: float
    passed: bool | None = Field(description="Null when the movement could not be decomposed.")


class DetectionAuditOut(ApiModel):
    """Why the day was flagged: both tests, with their inputs."""

    trigger: str = Field(description="raw, abnormal or both, as stored; raw for movements before v2.")
    qualifies: bool
    current_mode: Literal["raw", "abnormal", "union"]
    raw: RawTestOut
    abnormal: AbnormalTestOut


class SessionFrameOut(ApiModel):
    """The session's boundaries on the New York Stock Exchange calendar (early
    closes and holidays included), for placing evidence on a timeline."""

    previous: date
    prior_close: datetime
    open: datetime
    close: datetime
    next_open: datetime


class MovementCaseOut(ApiModel):
    ticker: TickerOut
    session: SessionFrameOut | None = Field(default=None, description="Null outside the exchange calendar.")
    movement: MovementOut
    detection: DetectionAuditOut
    benchmarks: list[ReferenceReturnOut]
    peers: list[ReferenceReturnOut] = Field(
        description="The ticker's peer set and how each did that session (prices only, "
        "refreshed nightly for the pre-warmed universe's peers)."
    )
    previous_date: date | None = Field(default=None, description="The ticker's previous movement.")
    next_date: date | None = Field(default=None, description="The ticker's next movement.")


class TickerFilingOut(ApiModel):
    accession: str
    form: str
    items: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    filing_date: date
    accepted_at: datetime | None = None
    url: str | None = None
    movement_dates: list[date] = Field(
        default_factory=list, description="Movements whose news window this filing falls in."
    )


class TickerFilingsOut(ApiModel):
    symbol: str
    filings: list[TickerFilingOut]
    note: str | None = None


class EarningsMovementOut(ApiModel):
    date: date
    daily_return: float
    abnormal_return: float | None = None
    evidence: EvidenceState | None = None


class EarningsEventOut(ApiModel):
    """An earnings release, from an 8-K with Item 2.02."""

    accession: str
    accepted_at: datetime | None
    filing_date: date
    session: date | None = Field(description="The first session whose prices could reflect it.")
    timing: Literal["before_open", "during_session", "after_close", "unknown"]
    url: str | None = None
    movement: EarningsMovementOut | None = Field(
        default=None, description="The movement on that session, if the day was flagged."
    )


class EarningsOut(ApiModel):
    symbol: str
    source: str = "sec_8k_item_2.02"
    events: list[EarningsEventOut]
    note: str


class PulseMoverOut(ApiModel):
    symbol: str
    company_name: str | None
    date: date
    daily_return: float
    abnormal_return: float | None
    sar: float | None
    dominant: str | None
    explanation: str = Field(description="The strongest linked evidence in one line, or why there is none.")
    evidence: EvidenceState


class SectorReturnOut(ApiModel):
    sector: str
    symbol: str
    total_return: float | None = Field(description="Compounded over the sessions; null without bars.")


class MarketPulseOut(ApiModel):
    sessions: list[date] = Field(description="The sessions covered, latest last.")
    movers: list[PulseMoverOut] = Field(description="Largest abnormal moves first.")
    featured: PulseMoverOut | None = Field(
        default=None,
        description="The move to show first: the largest recent move whose linked evidence clearly "
        "explains it (top score at least FEATURED_MIN_SCORE), looking back FEATURED_LOOKBACK_DAYS. "
        "Null when nothing qualifies.",
    )
    sectors: list[SectorReturnOut] = Field(description="Benchmarks' compounded return over the sessions.")
