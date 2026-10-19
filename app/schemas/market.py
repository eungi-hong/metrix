"""Response models for the ticker endpoint.

The shape is deliberately nested -- movement -> linked articles -> tier and
rationale -- rather than two parallel lists the caller has to join. The whole
point of the product is the *link* between a price move and the news, so the
link is what the payload is built around.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import ConfigDict, Field

from app.schemas.base import ApiModel

from app.models.enums import Direction, IngestStatus, NewsStatus, RelevanceTier

IngestState = Literal["ready", "ingesting", "refreshing", "failed"]
TimingOut = Literal["before_prior_close", "overnight", "intraday", "after_close", "later"]
TriggerOut = Literal["raw", "abnormal", "both"]
ComponentOut = Literal["market", "sector", "company"]
DriftLabelOut = Literal["continued", "held", "reversed"]
MovementSort = Literal["date", "abs_return", "abs_sar", "rvol"]


class PriceBarOut(ApiModel):
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


class ArticleOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    title: str | None
    source: str | None = Field(default=None, description="Publisher domain.")
    author: str | None
    published_at: datetime | None
    summary: str | None


class LinkedArticleOut(ApiModel):
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
    timing: TimingOut | None = Field(
        default=None,
        description="When it was published relative to the move's XNYS sessions "
        "(previous session P, the move's session T, the next session N): "
        "before_prior_close (at or before close(P)), overnight (close(P) to "
        "open(T)), intraday (open(T) to close(T)), after_close (close(T) to "
        "open(N)) or later. Only the first three can have caused T's "
        "close-to-close move. Null when unknown.",
    )
    timing_precision: Literal["time", "day"] | None = Field(
        default=None,
        description="'time' when the publication time is known; 'day' when only "
        "the date is, in which case `timing` is set only if the whole day falls "
        "in one bucket.",
    )
    role: Literal["primary", "contributing", "context"] | None = Field(
        default=None, description="Scorer v2 only: the cause, part of it, or background."
    )
    scorer_version: str | None = Field(default=None, description="v1 or v2; null before v2 existed.")


EvidenceState = Literal[
    "explained", "abstained", "no_evidence", "researching", "queued", "budget_deferred",
    "window_open", "retrying", "failed", "not_started",
]


class EvidenceStatusOut(ApiModel):
    """Where the movement's evidence stands and when it will be there."""

    state: EvidenceState
    detail: str
    window_closes_at: datetime | None = Field(
        default=None, description="When the movement's news window closes."
    )
    expected_by: datetime | None = Field(
        default=None,
        description="When research may next start: the queued job's start time, the "
        "spend cap's reset, or the next nightly run. Null when nothing is owed.",
    )


class FilingOut(ApiModel):
    """An SEC filing in the movement's window, with its session timing, and
    scorer v2's verdict on it when it was judged."""

    accession: str
    form: str
    items: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    accepted_at: datetime | None = None
    url: str | None = None
    timing: TimingOut | None = None
    timing_precision: Literal["time", "day"] | None = None
    relevance_tier: str | None = None
    relevance_score: float | None = None
    role: Literal["primary", "contributing", "context"] | None = None
    rationale: str | None = None


class DecompositionOut(ApiModel):
    """The day's return in parts: alpha + market + sector + abnormal = daily_return.

    From a market model with an orthogonalised sector factor, estimated on
    the sessions before the move. See README, "How a movement is defined".
    """

    model: Literal["market_sector", "market_only"]
    market_benchmark: str
    sector_benchmark: str | None = Field(
        default=None, description="Null for a market-only model (no mapped sector)."
    )
    estimation_sessions: int
    alpha: float = Field(description="Daily intercept over the estimation window.")
    beta_market: float
    beta_sector: float | None = Field(
        default=None, description="Loading on the sector's market-neutral return."
    )
    r_squared: float
    sigma_e: float = Field(description="Residual standard deviation, as a daily return.")
    market: float = Field(description="beta_market * the market's return that day.")
    sector: float = Field(description="beta_sector * the sector's market-neutral return that day.")
    expected: float = Field(description="alpha + market + sector.")
    abnormal: float = Field(description="daily_return - expected: the company-specific part.")
    sar: float | None = Field(
        default=None,
        description="Standardised abnormal return (forecast-error adjusted). A "
        "heuristic size in residual standard deviations, not a probability.",
    )
    dominant: ComponentOut = Field(description="The largest of market, sector, company by size.")
    note: str | None = Field(default=None, description="A caveat, e.g. why the model is market only.")


class LegsOut(ApiModel):
    """Where in the session the move happened. (1 + gap) * (1 + intraday) = 1 + daily_return."""

    gap: float = Field(description="Prior adjusted close to the (adjusted) open.")
    intraday: float = Field(description="Open to close.")


class VolumeAnalysisOut(ApiModel):
    relative_volume: float = Field(
        description="The day's volume over the median of the sessions before it."
    )
    lookback_sessions: int


class DriftOut(ApiModel):
    """What happened next: cumulative abnormal return after the move, with the
    model fixed at the move. Null until those sessions exist."""

    car_5: float | None = None
    car_20: float | None = None
    label_5: DriftLabelOut | None = None
    label_20: DriftLabelOut | None = None


class RegimeOut(ApiModel):
    """Volatility going into the move (the sessions before it, not including it)."""

    realized_vol: float | None = Field(default=None, description="Annualised.")
    percentile: float | None = Field(
        default=None, description="0 to 100, against the same measure over the past year."
    )
    window_sessions: int


class MovementOut(ApiModel):
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

    trigger: TriggerOut = Field(
        default="raw",
        description="Which detection test the day passed: raw (|r| >= max(floor, "
        "k * sigma)), abnormal (|SAR| >= MOVEMENT_ABNORMAL_K), or both.",
    )
    qualifies: bool = Field(
        default=True,
        description="False only with include_unqualified=true: a movement that no "
        "longer clears the threshold, kept because it was already researched.",
    )
    decomposition: DecompositionOut | None = Field(
        default=None, description="Null when the day could not be decomposed."
    )
    decomposition_warning: str | None = Field(
        default=None, description="Why there is no decomposition, when there is none."
    )
    legs: LegsOut | None = None
    volume_analysis: VolumeAnalysisOut | None = Field(
        default=None,
        description="Relative volume. (`volume` above is the day's share volume.)",
    )
    drift: DriftOut | None = None
    regime: RegimeOut | None = None
    filings: list[FilingOut] = Field(
        default_factory=list, description="SEC filings in the news window (when EDGAR is enabled)."
    )
    evidence: EvidenceStatusOut | None = Field(
        default=None, description="Why there is or is not evidence yet, and when there will be."
    )
    evidence_abstained: bool | None = Field(
        default=None, description="Scorer v2: true when it judged that nothing explains the move."
    )
    abstain_reason: str | None = None


class TickerOut(ApiModel):
    model_config = ConfigDict(from_attributes=True)

    symbol: str
    company_name: str | None
    sector: str | None
    industry: str | None
    exchange: str | None
    currency: str | None


class PriceRangeOut(ApiModel):
    """Summary of the stored price history, always present."""

    start: date | None
    end: date | None
    bars: int


class PaginationOut(ApiModel):
    limit: int
    offset: int
    total: int = Field(description="Movements matching the filters, before paging.")
    returned: int


class AppliedFiltersOut(ApiModel):
    start: date | None = None
    end: date | None = None
    min_magnitude: float | None = None
    direction: Direction | None = None
    tiers: list[RelevanceTier] | None = None
    triggers: list[TriggerOut] | None = None
    dominant: list[ComponentOut] | None = None
    min_sar: float | None = None
    sort: MovementSort = "date"
    include_unqualified: bool = False


class BenchmarkPointOut(ApiModel):
    date: date
    value: float = Field(description="Adjusted close rebased to 100 on `base_date`.")


class BenchmarkSeriesOut(ApiModel):
    """A benchmark's adjusted close, rebased for a chart overlay."""

    symbol: str
    role: Literal["market", "sector"]
    base_date: date | None
    points: list[BenchmarkPointOut]


class TickerDetailOut(ApiModel):
    """The ticker endpoint's payload."""

    status: IngestState = Field(
        description="'ready' -- data is fresh. 'ingesting' -- first-time fetch is "
        "running, movements will be empty. 'refreshing' -- stale data is shown "
        "while a refresh runs. 'failed' -- the last ingestion failed."
    )
    message: str | None = None
    job_id: int | None = Field(
        default=None,
        description="The queued ingestion job, when this request started or joined "
        "one. Follow it at GET /jobs/{job_id}.",
    )
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
    benchmarks: list[BenchmarkSeriesOut] | None = Field(
        default=None,
        description="With include_benchmarks=true: the market and sector "
        "benchmarks over the same dates as the ticker's bars, rebased to 100 on "
        "its first bar in range.",
    )
