"""SEC filings, and which movements they sit next to.

A filing is a primary source: it never changes once accepted, so it is
stored once (unique on accession number) and attached to every movement
whose news window it falls in, with its session timing. Under scorer v2 a
filing is also a scoring candidate, and the attachment then carries the
verdict (tier, score, role, rationale) the way an article link does.
"""

from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin


class Filing(Base, TimestampMixin):
    __tablename__ = "filings"
    __table_args__ = (sa.Index("ix_filings_ticker_accepted", "ticker_id", "accepted_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(sa.ForeignKey("tickers.id", ondelete="CASCADE"), nullable=False)
    cik: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    accession: Mapped[str] = mapped_column(sa.String(32), unique=True, nullable=False)
    form: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    filing_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    items: Mapped[list[str] | None] = mapped_column(JSONType)
    primary_document: Mapped[str | None] = mapped_column(sa.String(255))
    url: Mapped[str | None] = mapped_column(sa.Text)


class MovementFiling(Base, TimestampMixin):
    """A filing in a movement's window. The verdict fields are set only when
    scorer v2 judged it; `superseded_at` as for article links."""

    __tablename__ = "movement_filings"
    __table_args__ = (sa.UniqueConstraint("movement_id", "filing_id", name="uq_movement_filing"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    movement_id: Mapped[int] = mapped_column(sa.ForeignKey("movements.id", ondelete="CASCADE"), nullable=False, index=True)
    filing_id: Mapped[int] = mapped_column(sa.ForeignKey("filings.id", ondelete="CASCADE"), nullable=False)
    timing: Mapped[str | None] = mapped_column(sa.String(24))
    timing_precision: Mapped[str | None] = mapped_column(sa.String(8))
    relevance_tier: Mapped[str | None] = mapped_column(sa.String(16))
    relevance_score: Mapped[float | None] = mapped_column(sa.Float)
    role: Mapped[str | None] = mapped_column(sa.String(16))
    rationale: Mapped[str | None] = mapped_column(sa.Text)
    scored_by: Mapped[str | None] = mapped_column(sa.String(64))
    superseded_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    filing: Mapped[Filing] = relationship(lazy="joined")
