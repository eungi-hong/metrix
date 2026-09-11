"""Competitor and industry resolution for the Medium news tier.

Design note -- why an LLM call instead of a static map
-----------------------------------------------------
A hand-maintained ticker -> competitors table is accurate on the day it is
written and wrong a quarter later, and it only covers the tickers someone
thought to add. This app accepts any symbol yfinance knows about, so a static
map would silently degrade the Medium tier to nothing for most inputs.

yfinance supplies `sector` and `industry`, which is a real signal but too
coarse on its own: "Consumer Electronics" does not tell a search engine to look
for Samsung and Xiaomi. So the LLM is asked to name the peers, *grounded* in
the yfinance sector/industry so it is classifying rather than free-associating.

The cost objection is answered by caching: the result is stored on the ticker
row with a TTL (`PEERS_TTL_DAYS`), so it is one call per ticker per month, not
one per movement. If the LLM is unavailable the tier degrades to an
industry-keyword search rather than disappearing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import LLMError, MetrixError
from app.core.logging import get_logger
from app.models.market import Ticker
from app.services.llm import LLMClient

logger = get_logger(__name__)

_SYSTEM = (
    "You are an equity research assistant. Given a public company, you identify the "
    "other companies whose news most plausibly moves this company's stock price, and "
    "the industry themes that would appear in such coverage. Be specific and current. "
    "Prefer direct competitors and major suppliers or customers over loose sector peers."
)


class PeerCompany(BaseModel):
    name: str = Field(description="Company name as it appears in news coverage.")
    ticker: str | None = Field(default=None, description="Exchange ticker, if listed.")
    relationship: Literal["competitor", "supplier", "customer", "partner"]


class PeerSet(BaseModel):
    peers: list[PeerCompany] = Field(default_factory=list, max_length=8)
    industry_themes: list[str] = Field(
        default_factory=list,
        max_length=6,
        description="Industry-level topics whose news moves this company, e.g. "
        "'memory chip pricing', 'GLP-1 drug approvals'.",
    )


async def resolve_peers(
    session: AsyncSession, ticker: Ticker, llm: LLMClient
) -> PeerSet:
    """Peers for `ticker`, from cache when fresh, otherwise from the LLM."""
    if _cache_is_fresh(ticker):
        try:
            return PeerSet.model_validate(ticker.peers)
        except Exception:  # cached shape predates a schema change
            logger.warning("peers_cache_invalid", symbol=ticker.symbol)

    try:
        peer_set = await _resolve_via_llm(ticker, llm)
    except (LLMError, MetrixError) as exc:
        logger.warning("peers_llm_failed", symbol=ticker.symbol, error=str(exc))
        return _fallback(ticker)

    ticker.peers = peer_set.model_dump()
    ticker.peers_resolved_at = datetime.now(timezone.utc)
    await session.flush()
    logger.info(
        "peers_resolved",
        symbol=ticker.symbol,
        peers=[p.name for p in peer_set.peers],
    )
    return peer_set


async def _resolve_via_llm(ticker: Ticker, llm: LLMClient) -> PeerSet:
    descriptor = ", ".join(
        part
        for part in (
            f"symbol {ticker.symbol}",
            ticker.company_name,
            f"sector: {ticker.sector}" if ticker.sector else None,
            f"industry: {ticker.industry}" if ticker.industry else None,
        )
        if part
    )
    return await llm.parse_structured(
        system=_SYSTEM,
        user=(
            f"Company: {descriptor}.\n\n"
            "List up to 6 companies whose news moves this stock, and up to 4 industry "
            "themes. Return only companies you are confident about."
        ),
        output_model=PeerSet,
        max_tokens=1024,
    )


def _cache_is_fresh(ticker: Ticker) -> bool:
    if not ticker.peers or not ticker.peers_resolved_at:
        return False
    resolved = ticker.peers_resolved_at
    if resolved.tzinfo is None:
        resolved = resolved.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - resolved < timedelta(days=settings.peers_ttl_days)


def _fallback(ticker: Ticker) -> PeerSet:
    """No LLM: keep the tier alive using yfinance's own classification."""
    themes = [t for t in (ticker.industry, ticker.sector) if t]
    return PeerSet(peers=[], industry_themes=themes)
