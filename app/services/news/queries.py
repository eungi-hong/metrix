"""Query construction for the three relevance tiers.

Each tier asks a different question of the search index:

Easy    -- what happened at this company?
Medium  -- what happened at its competitors or in its industry?
Hard    -- what happened to the market as a whole?

The tiers are search *strategies*, not labels: which bucket surfaced an article
is recorded as `search_tier`, but the tier stored on the link is whatever the
scoring model decides the article actually is. A "macro" search regularly turns
up a company-specific story and vice versa.

Cache-sharing note: the Hard-tier request deliberately mentions no company, only
the sector and the date. That has to hold for every field of the request, not
just the query text, because the cache key is a hash of all of them -- including
`summary_query`, the question Exa summarizes each result against. Easy and
Medium summaries ask about this company's move; the Hard summary asks what in
the article would move the sector. Every ticker in the same sector on the same
date therefore produces a byte-identical Hard request, hits the same
`news_query_cache` row, and costs one search instead of N.

The price of that is a less targeted snippet: the scorer reads a sector-level
summary of a macro article rather than one written for this company's move.
The scorer still sees the company and the move in its own prompt, and judging
whether a macro event explains this particular move is its job anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from app.core.config import settings
from app.models.enums import RelevanceTier
from app.services.news.base import NewsSearchRequest, window_closes_at
from app.services.peers import PeerSet

SEARCH_TIER_EASY = "easy"
SEARCH_TIER_MEDIUM = "medium"
SEARCH_TIER_HARD = "hard"


@dataclass(frozen=True, slots=True)
class MovementContext:
    """Everything the query builders and the scorer need about one movement."""

    symbol: str
    company_name: str | None
    sector: str | None
    industry: str | None
    movement_date: date
    daily_return: float
    direction: str

    @property
    def display_name(self) -> str:
        return self.company_name or self.symbol

    @property
    def pct(self) -> str:
        return f"{self.daily_return:+.2%}"

    def describe(self) -> str:
        return (
            f"{self.display_name} ({self.symbol}) closed {self.pct} on "
            f"{self.movement_date.isoformat()}"
        )


def search_window(movement_date: date) -> tuple[datetime, datetime]:
    """The published-date range searched for a movement.

    Opens a few days early because the news that moves a stock frequently
    breaks before the session it moves in (an overnight filing, a weekend
    report), and closes a day late to catch same-evening follow-ups.
    """
    start = movement_date - timedelta(days=settings.news_window_days_before)
    end = movement_date + timedelta(days=settings.news_window_days_after)
    return (
        datetime.combine(start, time.min, tzinfo=timezone.utc),
        datetime.combine(end, time.max, tzinfo=timezone.utc),
    )


def news_window_closes_at(movement_date: date) -> datetime:
    """When a movement's news window closes and its enrichment becomes final."""
    _, end = search_window(movement_date)
    return window_closes_at(end)


def build_tier_queries(
    context: MovementContext, peers: PeerSet
) -> list[tuple[str, NewsSearchRequest]]:
    """`(search_tier, request)` pairs for one movement, in tier order."""
    start, end = search_window(context.movement_date)
    num = settings.max_candidates_per_tier
    company_summary = (
        f"Does this article explain why {context.display_name} stock moved "
        f"{context.pct} on {context.movement_date.isoformat()}?"
    )

    def request(query: str, summary_query: str) -> NewsSearchRequest:
        return NewsSearchRequest(
            query=query,
            start=start,
            end=end,
            num_results=num,
            summary_query=summary_query,
        )

    queries: list[tuple[str, NewsSearchRequest]] = [
        (SEARCH_TIER_EASY, request(_easy_query(context), company_summary)),
    ]

    medium = _medium_query(context, peers)
    if medium:
        queries.append((SEARCH_TIER_MEDIUM, request(medium, company_summary)))

    queries.append((SEARCH_TIER_HARD, hard_tier_request(context.sector, context.movement_date)))
    return queries


def hard_tier_request(sector: str | None, movement_date: date) -> NewsSearchRequest:
    """The Hard-tier search for any movement in `sector` on `movement_date`.

    A function of sector and date alone -- that is what lets every ticker in
    the sector share its cache row, and what lets the nightly run's
    `prewarm_sector_macro` job warm that row before any movement asks for it.
    """
    start, end = search_window(movement_date)
    sector = sector or HARD_TIER_DEFAULT_SECTOR
    return NewsSearchRequest(
        query=_hard_query(sector),
        start=start,
        end=end,
        num_results=settings.max_candidates_per_tier,
        summary_query=_hard_summary(sector, movement_date),
    )


def _easy_query(context: MovementContext) -> str:
    """Company-specific: earnings, launches, lawsuits, guidance, ratings."""
    return (
        f"{context.display_name} ({context.symbol}) company news: earnings results, "
        f"guidance, product launches, lawsuits, regulatory filings, executive changes, "
        f"analyst rating changes, or anything that would move the share price"
    )


def _medium_query(context: MovementContext, peers: PeerSet) -> str | None:
    """Competitor and industry: news about the neighbourhood, not the company."""
    names = [p.name for p in peers.peers][:6]
    themes = list(peers.industry_themes)[:4]
    if not names and not themes:
        themes = [t for t in (context.industry, context.sector) if t]
    if not names and not themes:
        return None

    parts: list[str] = []
    if names:
        parts.append(f"news about {', '.join(names)}")
    if themes:
        parts.append(f"developments in {', '.join(themes)}")
    return (
        f"{' and '.join(parts)} -- competitor earnings, guidance changes, pricing, "
        f"supply, demand or market-share shifts affecting the "
        f"{context.industry or context.sector or 'industry'} sector"
    )


# What the Hard tier searches for when yfinance gave the ticker no sector.
HARD_TIER_DEFAULT_SECTOR = "US equity"


def _hard_query(sector: str) -> str:
    """Macro / political: deliberately company-free, so the cache is shared."""
    return (
        f"macroeconomic and political news moving {sector} stocks: Federal Reserve "
        f"interest rate decisions, inflation and jobs data, tariffs and trade policy, "
        f"new regulation, government shutdowns, geopolitical conflict, and broad "
        f"market selloffs or rallies"
    )


def _hard_summary(sector: str, movement_date: date) -> str:
    """Company-free like the query: it is part of the cache key too."""
    return (
        f"Which macroeconomic, political or regulatory developments in this "
        f"article would move {sector} stocks around {movement_date.isoformat()}?"
    )


TIER_FOR_SEARCH: dict[str, RelevanceTier] = {
    SEARCH_TIER_EASY: RelevanceTier.EASY,
    SEARCH_TIER_MEDIUM: RelevanceTier.MEDIUM,
    SEARCH_TIER_HARD: RelevanceTier.HARD,
}
