"""Watchlists: named sets of symbols, optionally weighted, owned like conversations.

A watchlist belongs to a user (`user_id`) or, when AUTH_REQUIRED is off, to
an anonymous caller (`anonymous_key`, "anon:<ip>"). Holdings name symbols
rather than ticker rows, so a symbol Metrix has not fetched yet can be held.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class Watchlist(Base, TimestampMixin):
    __tablename__ = "watchlists"
    __table_args__ = (
        sa.Index("ix_watchlists_user_id", "user_id"),
        sa.Index("ix_watchlists_anonymous_key", "anonymous_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"))
    anonymous_key: Mapped[str | None] = mapped_column(sa.String(80))
    name: Mapped[str] = mapped_column(sa.String(80), nullable=False)

    holdings: Mapped[list["WatchlistHolding"]] = relationship(
        cascade="all, delete-orphan", order_by="WatchlistHolding.position", lazy="selectin"
    )


class WatchlistHolding(Base):
    __tablename__ = "watchlist_holdings"
    __table_args__ = (sa.UniqueConstraint("watchlist_id", "symbol", name="uq_watchlist_symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(
        sa.ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    # Optional; all holdings of a list are weighted or none are (equal weight).
    weight: Mapped[float | None] = mapped_column(sa.Float)
    position: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
