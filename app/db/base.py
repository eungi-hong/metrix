"""Declarative base and shared column types.

JSON columns are declared portably (`JSONB` on Postgres, plain `JSON`
elsewhere) so the same models back the Postgres runtime and the in-memory
SQLite database used by the endpoint smoke tests.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = sa.JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSONType, list: JSONType}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )
