"""Ticker, price history, and detected movements."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin
from app.models.enums import Direction, IngestStatus, NewsStatus, sa_enum


class Ticker(Base, TimestampMixin):
    """A public company we track.

    `sector`/`industry` come from yfinance and are the grounding for the
    Medium (competitor/industry) news tier; `peers` caches the LLM-resolved
    competitor list so we pay for that reasoning once per ticker per TTL.
    """

    __tablename__ = "tickers"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(sa.String(16), unique=True, index=True)
    company_name: Mapped[str | None] = mapped_column(sa.String(255))
    sector: Mapped[str | None] = mapped_column(sa.String(128))
    industry: Mapped[str | None] = mapped_column(sa.String(128))
    exchange: Mapped[str | None] = mapped_column(sa.String(32))
    currency: Mapped[str | None] = mapped_column(sa.String(8))

    peers: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    peers_resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    # Ingestion bookkeeping -- drives fetch-if-missing / fetch-if-stale.
    ingest_status: Mapped[IngestStatus] = mapped_column(
        sa_enum(IngestStatus, "ingest_status"),
        default=IngestStatus.PENDING,
        nullable=False,
    )
    ingest_error: Mapped[str | None] = mapped_column(sa.Text)
    last_ingested_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    ingest_started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    prices: Mapped[list["PriceBar"]] = relationship(
        back_populates="ticker", cascade="all, delete-orphan", lazy="noload"
    )
    movements: Mapped[list["Movement"]] = relationship(
        back_populates="ticker", cascade="all, delete-orphan", lazy="noload"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Ticker {self.symbol}>"


class PriceBar(Base):
    """One daily OHLCV bar.

    Prices are NUMERIC, not float: they are money, and exact round-tripping
    matters more than arithmetic speed here. Returns are computed in float
    space inside the detector, which is the only place we do math on them.
    """

    __tablename__ = "price_history"
    __table_args__ = (
        sa.UniqueConstraint("ticker_id", "date", name="uq_price_ticker_date"),
        sa.Index("ix_price_ticker_date", "ticker_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        sa.ForeignKey("tickers.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(sa.Date, nullable=False)

    open: Mapped[float | None] = mapped_column(sa.Numeric(18, 6))
    high: Mapped[float | None] = mapped_column(sa.Numeric(18, 6))
    low: Mapped[float | None] = mapped_column(sa.Numeric(18, 6))
    close: Mapped[float | None] = mapped_column(sa.Numeric(18, 6))
    adj_close: Mapped[float] = mapped_column(sa.Numeric(18, 6), nullable=False)
    volume: Mapped[int | None] = mapped_column(sa.BigInteger)

    ticker: Mapped[Ticker] = relationship(back_populates="prices", lazy="noload")


class Movement(Base, TimestampMixin):
    """A day flagged as a major move.

    Everything needed to re-derive the verdict is stored alongside it
    (`rolling_std`, `threshold`, and the three parameters in force at
    detection time), so a result stays explainable even after the config
    changes underneath it.
    """

    __tablename__ = "movements"
    __table_args__ = (
        sa.UniqueConstraint("ticker_id", "date", name="uq_movement_ticker_date"),
        sa.Index("ix_movement_ticker_date", "ticker_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker_id: Mapped[int] = mapped_column(
        sa.ForeignKey("tickers.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(sa.Date, nullable=False)

    daily_return: Mapped[float] = mapped_column(sa.Float, nullable=False)
    abs_return: Mapped[float] = mapped_column(sa.Float, nullable=False, index=True)
    direction: Mapped[Direction] = mapped_column(
        sa_enum(Direction, "direction"), nullable=False, index=True
    )

    prev_adj_close: Mapped[float] = mapped_column(sa.Numeric(18, 6), nullable=False)
    adj_close: Mapped[float] = mapped_column(sa.Numeric(18, 6), nullable=False)
    volume: Mapped[int | None] = mapped_column(sa.BigInteger)

    # --- audit trail for the detection decision ---
    rolling_std: Mapped[float | None] = mapped_column(sa.Float)
    threshold: Mapped[float] = mapped_column(sa.Float, nullable=False)
    threshold_source: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    detector_k: Mapped[float] = mapped_column(sa.Float, nullable=False)
    detector_window: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    detector_floor: Mapped[float] = mapped_column(sa.Float, nullable=False)

    news_status: Mapped[NewsStatus] = mapped_column(
        sa_enum(NewsStatus, "news_status"), default=NewsStatus.PENDING, nullable=False
    )
    news_fetched_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    ticker: Mapped[Ticker] = relationship(back_populates="movements", lazy="noload")
    news_links: Mapped[list["MovementNewsLink"]] = relationship(
        back_populates="movement", cascade="all, delete-orphan", lazy="noload"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Movement {self.ticker_id} {self.date} {self.daily_return:+.2%}>"
