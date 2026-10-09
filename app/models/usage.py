"""The spend ledger and the daily spend cap.

Both live in Postgres, not Redis, though Redis arrives in this phase for the
hot, approximate counters. Money has to be durable and auditable, and the cap
has to hold when Redis is down. `app.services.spend` holds the semantics.

`usage_events` is the audit trail: one row per billable external call, never
updated. `spend_daily` is the running total the cap is enforced against, one
row per UTC date, updated by conditional UPDATEs so concurrent calls cannot
jointly overspend it (the pattern of `queue.take_enrichment_budget`).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.core.context import CallClass
from app.db.base import Base
from app.models.enums import sa_enum

# Dollars to the micro-dollar: a single small LLM call costs fractions of a cent.
Money = sa.Numeric(14, 6)


class UsageEvent(Base):
    """One billable external call, with who it was for and what it cost."""

    __tablename__ = "usage_events"
    __table_args__ = (
        sa.Index("ix_usage_events_created_at", "created_at"),
        sa.Index("ix_usage_events_user_id_created_at", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # From the application clock, like the queue's timestamps, so it lands on
    # the same UTC date as the `spend_daily` row the cost was added to.
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    # What the call was for: chat, relevance, peers, search.
    operation: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(sa.String(128))
    input_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    output_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(sa.Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Money, nullable=False)
    # True when the cost is not the provider's own figure: Exa returned no
    # costDollars, or the model has no entry in LLM_PRICES_JSON.
    cost_estimated: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    # No foreign key yet: users arrive in the next stage.
    user_id: Mapped[int | None] = mapped_column(sa.Integer)
    job_id: Mapped[int | None] = mapped_column(sa.ForeignKey("jobs.id", ondelete="SET NULL"))
    call_class: Mapped[CallClass] = mapped_column(
        sa_enum(CallClass, "call_class"), nullable=False
    )


class SpendDaily(Base):
    """The day's running spend, and what in-flight calls have reserved.

    `spent_usd` is settled cost, and always equals the sum of that day's
    `usage_events`. `reserved_usd` is what calls in flight have set aside
    before calling; a call that dies without settling leaves its reservation
    behind, which overcounts until the next day's row starts at zero: the safe
    direction for a cap. The `background_` columns are the same figures for
    background calls only, the share that keeps a reserve for users.

    The `*_logged_at` columns make each alert fire at most once a day.
    """

    __tablename__ = "spend_daily"

    day: Mapped[date] = mapped_column(sa.Date, primary_key=True)
    spent_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), nullable=False)
    background_spent_usd: Mapped[Decimal] = mapped_column(
        Money, default=Decimal(0), nullable=False
    )
    reserved_usd: Mapped[Decimal] = mapped_column(Money, default=Decimal(0), nullable=False)
    background_reserved_usd: Mapped[Decimal] = mapped_column(
        Money, default=Decimal(0), nullable=False
    )
    alert_logged_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    interactive_cap_logged_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )
    background_cap_logged_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
