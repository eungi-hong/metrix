"""Ticker, price history, and detected movements."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin
from app.models.enums import Direction, IngestStatus, NewsStatus, sa_enum

# Decimal places prices are stored to. Anything compared against a stored
# price must be rounded to this first, or it never compares equal.
PRICE_DECIMALS = 6


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
    # Whether that error can never succeed on a retry (an unknown symbol, a
    # rejected key). Pre-warming skips such tickers instead of paying for the
    # same failure every night.
    ingest_error_permanent: Mapped[bool] = mapped_column(
        sa.Boolean, default=False, server_default=sa.false(), nullable=False
    )
    last_ingested_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    ingest_started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    # A market or sector benchmark (SPY, XLK). Its prices are refreshed every
    # night as data for the decomposition; it is not ranked by demand or
    # enriched by the nightly run. A user can still ask for it as a ticker.
    is_benchmark: Mapped[bool] = mapped_column(
        sa.Boolean, default=False, server_default=sa.false(), nullable=False
    )

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

    open: Mapped[float | None] = mapped_column(sa.Numeric(18, PRICE_DECIMALS))
    high: Mapped[float | None] = mapped_column(sa.Numeric(18, PRICE_DECIMALS))
    low: Mapped[float | None] = mapped_column(sa.Numeric(18, PRICE_DECIMALS))
    close: Mapped[float | None] = mapped_column(sa.Numeric(18, PRICE_DECIMALS))
    adj_close: Mapped[float] = mapped_column(sa.Numeric(18, PRICE_DECIMALS), nullable=False)
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

    prev_adj_close: Mapped[float] = mapped_column(sa.Numeric(18, PRICE_DECIMALS), nullable=False)
    adj_close: Mapped[float] = mapped_column(sa.Numeric(18, PRICE_DECIMALS), nullable=False)
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
    # When the news window stops accepting new articles (window end plus
    # NEWS_WINDOW_GRACE_HOURS). Enrichment that runs before this instant leaves
    # the movement PARTIAL rather than COMPLETE.
    news_window_closes_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    # Every enrichment attempt, successful or not. Caps automatic retries of a
    # movement that keeps failing, so it stops costing money on every run.
    news_attempts: Mapped[int] = mapped_column(
        sa.Integer, default=0, server_default="0", nullable=False
    )
    # "calendar" or "sessions" (NEWS_WINDOW_MODE): fixed once the movement is
    # first enriched, so a re-enrichment repeats the same, cached, searches.
    # NULL is a movement enriched before the setting existed: calendar.
    news_window_mode: Mapped[str | None] = mapped_column(sa.String(16))

    # --- detection modes (app.services.detection) ---
    # Which test the day passed: raw, abnormal or both. NULL predates v2: raw.
    trigger: Mapped[str | None] = mapped_column(sa.String(16))
    # False for a stored movement that no longer clears the threshold but has
    # links or a completed enrichment: paid work is kept, and hidden, rather
    # than deleted. A movement with neither is deleted instead.
    qualifies: Mapped[bool] = mapped_column(
        sa.Boolean, default=True, server_default=sa.true(), nullable=False
    )

    # --- return decomposition (app.services.decomposition); NULL when the day
    # could not be decomposed, with the reason in decomposition_warning ---
    decomposition_model: Mapped[str | None] = mapped_column(sa.String(16))
    market_benchmark: Mapped[str | None] = mapped_column(sa.String(16))
    sector_benchmark: Mapped[str | None] = mapped_column(sa.String(16))
    estimation_sessions: Mapped[int | None] = mapped_column(sa.Integer)
    alpha: Mapped[float | None] = mapped_column(sa.Float)
    beta_market: Mapped[float | None] = mapped_column(sa.Float)
    beta_sector: Mapped[float | None] = mapped_column(sa.Float)
    r_squared: Mapped[float | None] = mapped_column(sa.Float)
    sigma_e: Mapped[float | None] = mapped_column(sa.Float)
    market_component: Mapped[float | None] = mapped_column(sa.Float)
    sector_component: Mapped[float | None] = mapped_column(sa.Float)
    expected_return: Mapped[float | None] = mapped_column(sa.Float)
    abnormal_return: Mapped[float | None] = mapped_column(sa.Float)
    sar: Mapped[float | None] = mapped_column(sa.Float)
    dominant_component: Mapped[str | None] = mapped_column(sa.String(16))
    decomposition_warning: Mapped[str | None] = mapped_column(sa.Text)

    # --- per-movement analytics (app.services.analytics) ---
    gap_return: Mapped[float | None] = mapped_column(sa.Float)
    intraday_return: Mapped[float | None] = mapped_column(sa.Float)
    relative_volume: Mapped[float | None] = mapped_column(sa.Float)
    car_5: Mapped[float | None] = mapped_column(sa.Float)
    car_20: Mapped[float | None] = mapped_column(sa.Float)
    drift_5_label: Mapped[str | None] = mapped_column(sa.String(16))
    drift_20_label: Mapped[str | None] = mapped_column(sa.String(16))
    realized_vol: Mapped[float | None] = mapped_column(sa.Float)
    realized_vol_percentile: Mapped[float | None] = mapped_column(sa.Float)
    analytics_computed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    ticker: Mapped[Ticker] = relationship(back_populates="movements", lazy="noload")
    news_links: Mapped[list["MovementNewsLink"]] = relationship(
        back_populates="movement", cascade="all, delete-orphan", lazy="noload"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Movement {self.ticker_id} {self.date} {self.daily_return:+.2%}>"
