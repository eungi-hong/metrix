"""News articles, their links to movements, and the raw-response cache."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin
from app.models.enums import RelevanceTier, sa_enum
from app.models.market import Movement

# Tracking parameters carried by share links; stripping them stops the same
# article arriving from two searches and being stored twice.
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref")


def normalize_url(url: str) -> str:
    """Canonicalize a URL for deduplication (scheme, host case, tracking params)."""
    parts = urlsplit(url.strip())
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = "&".join(
        piece
        for piece in parts.query.split("&")
        if piece and not piece.lower().startswith(_TRACKING_PREFIXES)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", netloc, path, query, ""))


def url_fingerprint(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


class NewsArticle(Base, TimestampMixin):
    """A fetched article, stored once and reused across movements and tickers.

    Deduplicated on a normalized-URL hash rather than the raw URL so that the
    same story surfaced by the company search and by the macro search collapses
    into a single row -- which is also what makes re-ingestion idempotent.
    """

    __tablename__ = "news_articles"
    __table_args__ = (
        sa.Index("ix_news_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    url_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)

    title: Mapped[str | None] = mapped_column(sa.Text)
    source_domain: Mapped[str | None] = mapped_column(sa.String(255), index=True)
    author: Mapped[str | None] = mapped_column(sa.String(255))
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    summary: Mapped[str | None] = mapped_column(sa.Text)
    content: Mapped[str | None] = mapped_column(sa.Text)

    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    links: Mapped[list["MovementNewsLink"]] = relationship(
        back_populates="article", cascade="all, delete-orphan", lazy="noload"
    )


class MovementNewsLink(Base, TimestampMixin):
    """Why we believe this article explains this movement.

    `search_tier` records which query surfaced the article; `relevance_tier` is
    the LLM's verdict about what the article actually *is*. They disagree often
    enough to be worth keeping apart -- a macro-flavoured search routinely
    turns up a company-specific story, and vice versa.
    """

    __tablename__ = "movement_news_links"
    __table_args__ = (
        sa.UniqueConstraint("movement_id", "article_id", name="uq_link_movement_article"),
        sa.Index("ix_link_movement_tier", "movement_id", "relevance_tier"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    movement_id: Mapped[int] = mapped_column(
        sa.ForeignKey("movements.id", ondelete="CASCADE"), nullable=False
    )
    article_id: Mapped[int] = mapped_column(
        sa.ForeignKey("news_articles.id", ondelete="CASCADE"), nullable=False
    )

    relevance_tier: Mapped[RelevanceTier] = mapped_column(
        sa_enum(RelevanceTier, "relevance_tier"), nullable=False
    )
    relevance_score: Mapped[float] = mapped_column(sa.Float, nullable=False)
    rationale: Mapped[str | None] = mapped_column(sa.Text)
    search_tier: Mapped[str | None] = mapped_column(sa.String(16))
    scored_by: Mapped[str | None] = mapped_column(sa.String(64))

    movement: Mapped["Movement"] = relationship(back_populates="news_links", lazy="noload")
    article: Mapped[NewsArticle] = relationship(back_populates="links", lazy="joined")


class NewsQueryCache(Base):
    """Raw provider responses, keyed by the exact query that produced them.

    News searches are the expensive, rate-limited part of ingestion and their
    results for a *past* date window never change. Caching the raw payload
    means re-running ingestion during development costs nothing, and two
    tickers in the same sector share their macro-tier searches.
    """

    __tablename__ = "news_query_cache"

    id: Mapped[int] = mapped_column(primary_key=True)
    cache_key: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    query: Mapped[str] = mapped_column(sa.Text, nullable=False)
    params: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    response: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
