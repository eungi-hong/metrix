"""Tests for running behind a host rather than docker compose: database URLs
in the forms hosts hand out."""

from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@db:5432/metrix", "postgresql+asyncpg://u:p@db:5432/metrix"),
        ("postgres://u:p@db:5432/metrix", "postgresql+asyncpg://u:p@db:5432/metrix"),
        ("postgresql+asyncpg://u:p@db:5432/metrix", "postgresql+asyncpg://u:p@db:5432/metrix"),
        ("sqlite+aiosqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
    ],
)
def test_database_url_is_upgraded_to_the_async_driver(given, expected):
    assert Settings(database_url=given).database_url == expected


def test_only_the_scheme_is_rewritten():
    url = "postgres://u:postgres://x@db:5432/postgres"
    assert Settings(database_url=url).database_url == (
        "postgresql+asyncpg://u:postgres://x@db:5432/postgres"
    )
