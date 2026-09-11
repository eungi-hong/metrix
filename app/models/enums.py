"""Enumerations shared by the ORM models and the Pydantic schemas.

Stored as VARCHAR + CHECK (`native_enum=False`) rather than a Postgres ENUM
type: adding a value later is a no-op instead of an `ALTER TYPE` migration, and
the same DDL works on SQLite for tests.
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
    PENDING = "pending"
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
