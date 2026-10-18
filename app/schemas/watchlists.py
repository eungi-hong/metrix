"""Watchlist requests and responses."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field, field_validator, model_validator

from app.schemas.base import ApiModel

from app.schemas.market import EvidenceState


class HoldingIn(ApiModel):
    symbol: str = Field(min_length=1, max_length=16)
    weight: float | None = Field(default=None, gt=0, le=1_000_000, description="Any positive scale; normalised.")


class WatchlistIn(ApiModel):
    name: str = Field(min_length=1, max_length=80)
    holdings: list[HoldingIn] = Field(default_factory=list, max_length=50)

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value

    @model_validator(mode="after")
    def _all_or_no_weights(self) -> "WatchlistIn":
        weighted = [h.weight is not None for h in self.holdings]
        if any(weighted) and not all(weighted):
            raise ValueError("give every holding a weight, or none (equal weights)")
        return self


class HoldingOut(ApiModel):
    symbol: str
    weight: float = Field(description="Normalised: the holdings' weights sum to 1.")


class WatchlistOut(ApiModel):
    id: int
    name: str
    weighted: bool = Field(description="False: equal weights.")
    holdings: list[HoldingOut]
    created_at: datetime
    updated_at: datetime


class DigestMoveOut(ApiModel):
    date: date
    daily_return: float
    abnormal_return: float | None = None
    evidence: EvidenceState
    explanation: str


class DigestHoldingOut(ApiModel):
    symbol: str
    weight: float
    has_data: bool
    period_return: float | None = Field(description="Compounded over the sessions; null without stored bars.")
    contribution: float | None = Field(description="weight x period_return, with weights over holdings that have data.")
    moves: list[DigestMoveOut] = Field(description="Flagged movements in the period, newest first.")


class WatchlistDigestOut(ApiModel):
    id: int
    name: str
    sessions: list[date]
    period_return: float | None = Field(description="Sum of the contributions.")
    holdings: list[DigestHoldingOut]
    missing: list[str] = Field(
        default_factory=list,
        description="Holdings with no stored prices; GET /tickers/{symbol} fetches them.",
    )
