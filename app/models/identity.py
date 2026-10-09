"""Users and the API keys they call with.

Identity is API keys that we issue, not a login system or an OAuth provider.
That is enough to attribute usage, enforce per-user quotas and own
conversations, and it keeps one place, `app.api.deps.CurrentUser`, where a
later move to JWT or OAuth would land.

Users are disabled, never deleted: the spend ledger, jobs and conversations
keep pointing at them, so the history stays attributable.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import sa_enum


class Plan(StrEnum):
    """What a caller may do, and how much of it. Limits per plan come in with quotas.

    `anonymous` is never stored: it is the plan of an unauthenticated caller
    when AUTH_REQUIRED is off.
    """

    ANONYMOUS = "anonymous"
    FREE = "free"
    PRO = "pro"
    INTERNAL = "internal"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str | None] = mapped_column(sa.String(320), unique=True)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    plan: Mapped[Plan] = mapped_column(sa_enum(Plan, "user_plan"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    disabled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    @property
    def disabled(self) -> bool:
        return self.disabled_at is not None


class ApiKey(Base):
    """One key. Only its SHA-256 is stored; the key itself is shown once, at creation.

    `prefix` is the start of the key (`mtx_` and eight random characters). It
    is not secret: it is how a key is looked up, and how it is told apart in
    lists and logs without revealing it.
    """

    __tablename__ = "api_keys"
    __table_args__ = (sa.Index("ix_api_keys_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), nullable=False)
    prefix: Mapped[str] = mapped_column(sa.String(16), unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    label: Mapped[str | None] = mapped_column(sa.String(200))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    # Refreshed at most once per API_KEY_TOUCH_INTERVAL_MINUTES, not per request.
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
