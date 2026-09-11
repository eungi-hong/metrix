"""Response models for the ticker endpoint.

The shape is deliberately nested -- movement -> linked articles -> tier and
rationale -- rather than two parallel lists the caller has to join. The whole
point of the product is the *link* between a price move and the news, so the
link is what the payload is built around.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Direction, IngestStatus, NewsStatus, RelevanceTier

IngestState = Literal["ready", "ingesting", "refreshing", "failed"]


class PriceBarOut(BaseModel):
    """One daily OHLCV bar.

    Returned only when `include_prices=true`: a year of bars is ~250 rows, which
    would quadruple the size of every movement query that does not need them.
    """

    model_config = ConfigDict(from_attributes=True)

    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float = Field(
        description="Split- and dividend-adjusted close. Returns are computed from this."
    )
    volume: int | None


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    title: str | None
    source: str | None = Field(default=None, description="Publisher domain.")
    author: str | None
    published_at: datetime | None
    summary: str | None


class LinkedArticleOut(BaseModel):
    """An article together with the verdict connecting it to the movement."""

    article: ArticleOut
    relevance_tier: RelevanceTier = Field(
        description="What the article is: easy (company), medium (competitor/"
        "industry), or hard (macro/political)."
    )
    relevance_score: float = Field(ge=0.0, le=1.0)
    rationale: str | None = Field(
        default=None, description="One line on why this article explains the move."
    )
    search_tier: str | None = Field(
        default=None, description="Which tier's search surfaced the article."
    )


class MovementOut(BaseModel):
    """One detected major movement, with the audit trail for the decision."""

    id: int
    date: date
    daily_return: float = Field(description="Decimal fraction, e.g. -0.0412 = -4.12%.")
    daily_return_pct: float = Field(description="Same figure in percent, for display.")
    direction: Direction
    prev_adj_close: float
    adj_close: float
    volume: int | None

    threshold: float = Field(description="The bar this day had to clear.")
    threshold_source: str = Field(
        description="'floor' if the flat minimum bound the day, 'volatility' if "
        "k * rolling stdev did."
    )
    rolling_std: float | None = Field(
        default=None, description="Stdev of the N returns before this day."
    )
    sigma_multiple: float | None = Field(
        default=None, description="Size of the move in standard deviations."
    )
    detector_k: float
    detector_window: int
    detector_floor: float

    news_status: NewsStatus
    news: list[LinkedArticleOut] = Field(default_factory=list)


class TickerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    company_name: str | None
    sector: str | None
    industry: str | None
    exchange: str | None
    currency: str | None


class PriceRangeOut(BaseModel):
    """Summary of the stored price history, always present."""

    start: date | None
    end: date | None
    bars: int


class PaginationOut(BaseModel):
    limit: int
    offset: int
    total: int = Field(description="Movements matching the filters, before paging.")
    returned: int


class AppliedFiltersOut(BaseModel):
    start: date | None = None
    end: date | None = None
    min_magnitude: float | None = None
    direction: Direction | None = None
    tiers: list[RelevanceTier] | None = None


class TickerDetailOut(BaseModel):
    """The ticker endpoint's payload."""

    status: IngestState = Field(
        description="'ready' -- data is fresh. 'ingesting' -- first-time fetch is "
        "running, movements will be empty. 'refreshing' -- stale data is shown "
        "while a refresh runs. 'failed' -- the last ingestion failed."
    )
    message: str | None = None
    ticker: TickerOut
    ingest_status: IngestStatus
    last_ingested_at: datetime | None
    price_range: PriceRangeOut
    prices: list[PriceBarOut] | None = Field(
        default=None,
        description="The daily bars themselves, honouring `start`/`end`. "
        "Present only when requested with `include_prices=true`.",
    )
    filters: AppliedFiltersOut
    pagination: PaginationOut
    movements: list[MovementOut]
    warnings: list[str] = Field(default_factory=list)
