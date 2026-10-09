"""Tests for running behind a host rather than docker compose: database URLs
in the forms hosts hand out, CORS for preview deployments, the admin view
of the proxy headers that TRUSTED_PROXY_COUNT is measured with, and the
worker's wait for the API's migrations."""

from __future__ import annotations

import sys
from pathlib import Path

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


# ---------------------------------------------------- proxy diagnostics


async def request_info(headers: dict[str, str]):
    """GET /admin/request-info as if the socket peer were the host's proxy."""
    transport = ASGITransport(app=create_app(), client=("10.0.0.7", 51000))
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.get("/admin/request-info", headers=headers)


@pytest.fixture
def admin_token(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "s3cret")


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}])
async def test_request_info_needs_the_admin_token(admin_token, headers):
    response = await request_info({**headers, "X-Forwarded-For": "1.2.3.4"})
    assert response.status_code == 401
    assert "1.2.3.4" not in response.text


async def test_request_info_is_off_while_no_admin_token_is_set(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", None)
    assert (await request_info({"X-Forwarded-For": "1.2.3.4"})).status_code == 503


async def test_request_info_shows_the_peer_and_each_candidate_client(admin_token, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_count", 1)
    response = await request_info(
        {"X-Admin-Token": "s3cret", "X-Forwarded-For": "6.6.6.6, 203.0.113.9", "X-Real-IP": "203.0.113.9"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["peer"] == "10.0.0.7"
    assert body["hops"] == ["6.6.6.6", "203.0.113.9"]
    assert body["x_real_ip"] == "203.0.113.9"
    assert body["trusted_proxy_count"] == 1
    assert body["client_ip"] == "203.0.113.9"  # the forged 6.6.6.6 is ignored
    assert body["client_ip_by_trusted_count"] == {"0": "10.0.0.7", "1": "203.0.113.9", "2": "6.6.6.6"}
    assert "s3cret" not in response.text


# ------------------------------------------------- waiting for migrations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from wait_for_migrations import Schema, compare, script_directory  # noqa: E402


@pytest.fixture(scope="module")
def migrations():
    return script_directory()


def test_the_repository_has_a_single_migration_head(migrations):
    # Two heads means parallel branches each added a migration; deploys
    # would migrate to neither. Merge them before shipping.
    assert len(migrations.get_heads()) == 1


def test_a_database_at_head_is_ready(migrations):
    assert compare(set(migrations.get_heads()), migrations) is Schema.READY


def test_an_unmigrated_or_older_database_is_behind(migrations):
    head = migrations.get_revision(migrations.get_current_head())
    assert compare(set(), migrations) is Schema.BEHIND
    assert compare({head.down_revision}, migrations) is Schema.BEHIND


def test_a_database_newer_than_the_code_is_a_rollback_and_ready(migrations):
    assert compare({"f00dfacecafe"}, migrations) is Schema.AHEAD
