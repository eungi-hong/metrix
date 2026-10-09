"""Tests for running behind a host rather than docker compose: database URLs
in the forms hosts hand out, and CORS for preview deployments."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.main import create_app

PRODUCTION = "https://metrix.vercel.app"
PREVIEW_REGEX = r"^https://metrix-[a-z0-9-]+-eungi-hong\.vercel\.app$"


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


# ------------------------------------------------------------------ CORS


async def preflight(origin: str) -> dict[str, str]:
    """The CORS headers a browser's preflight from `origin` gets back.
    Answered by the middleware alone, so no database is needed."""
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as http:
        response = await http.options(
            "/tickers/NVDA",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
    return {k: v for k, v in response.headers.items() if k.startswith("access-control-")}


@pytest.fixture
def vercel_origins(monkeypatch):
    monkeypatch.setattr(settings, "cors_allowed_origins", PRODUCTION)
    monkeypatch.setattr(settings, "cors_allowed_origin_regex", PREVIEW_REGEX)


@pytest.mark.parametrize(
    "origin",
    [PRODUCTION, "https://metrix-git-deploy-live-eungi-hong.vercel.app", "https://metrix-4f2k9a1-eungi-hong.vercel.app"],
)
async def test_production_and_preview_origins_get_cors_headers(vercel_origins, origin):
    headers = await preflight(origin)
    assert headers["access-control-allow-origin"] == origin
    assert "authorization" in headers["access-control-allow-headers"].lower()


@pytest.mark.parametrize(
    "origin",
    [
        "https://someone-else.vercel.app",
        "https://metrix-x-someone-else.vercel.app",
        "https://metrix-x-eungi-hong.vercel.app.evil.com",  # the regex is matched in full
        "http://metrix-x-eungi-hong.vercel.app",
    ],
)
async def test_other_origins_get_no_cors_headers(vercel_origins, origin):
    assert "access-control-allow-origin" not in await preflight(origin)


async def test_the_regex_alone_turns_cors_on(monkeypatch):
    monkeypatch.setattr(settings, "cors_allowed_origins", "")
    monkeypatch.setattr(settings, "cors_allowed_origin_regex", PREVIEW_REGEX)
    origin = "https://metrix-abc123-eungi-hong.vercel.app"
    assert (await preflight(origin))["access-control-allow-origin"] == origin


async def test_no_cors_headers_by_default(monkeypatch):
    monkeypatch.setattr(settings, "cors_allowed_origins", "")
    monkeypatch.setattr(settings, "cors_allowed_origin_regex", "")
    assert "access-control-allow-origin" not in await preflight(PRODUCTION)


def test_an_invalid_origin_regex_fails_at_startup():
    with pytest.raises(ValidationError, match="CORS_ALLOWED_ORIGIN_REGEX"):
        Settings(cors_allowed_origin_regex="^https://(metrix")
