"""Enumerations shared by the ORM models and the Pydantic schemas.

Stored as plain VARCHAR (`native_enum=False`) rather than a Postgres ENUM
type: adding a value later is a no-op instead of an `ALTER TYPE` migration, and
the same DDL works on SQLite for tests. There is no CHECK constraint either --
SQLAlchemy 2.0 only emits one with `create_constraint=True` -- so the values
are enforced by the ORM, not the database.
"""

from __future__ import annotations

import enum
from enum import StrEnum

import sqlalchemy as sa


class IngestStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class NewsStatus(StrEnum):
    """Where a movement's news enrichment stands.

    PARTIAL -- enrichment ran, but before the news window closed, so articles
    published later in the window may be missing. It is re-enriched once the
    window closes. COMPLETE is reserved for enrichment that ran after that.
    """

    PENDING = "pending"
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"


class Direction(StrEnum):
    UP = "up"
    DOWN = "down"


class RelevanceTier(StrEnum):
    """How an article explains a movement.

    EASY   -- about the company itself (earnings, launches, lawsuits).
    MEDIUM -- about a competitor, supplier, or the industry at large.
    HARD   -- macro / political / regulatory, company need not be mentioned.
    """

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


def sa_enum(enum_cls: type[enum.Enum], name: str) -> sa.Enum:
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=16,
        values_callable=lambda e: [m.value for m in e],
    )
