"""Tests for scripts/smoke.py, offline: a fake API behind httpx.MockTransport.

The error bodies the fake API returns are built from the app's own exceptions
and error schema, so if the real 429 or 404 changes shape, these tests fail
rather than the smoke test failing against production.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.core.errors import RateLimited, SymbolNotListed
from app.core.symbols import is_valid_symbol
from app.schemas.chat import ErrorOut
from app.services.limits import daily_result
from app.services.quotas import Quota

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import smoke  # noqa: E402

API = "https://api.metrix.test"
KEY = "mtx_supersecretdemokey_0123456789"
ORIGIN = "https://metrix.vercel.app"

Handler = Callable[[httpx.Request], httpx.Response]


def config(**overrides: object) -> smoke.Config:
    values: dict[str, object] = {"url": API, "api_key": KEY, "frontend_origin": ORIGIN}
    return smoke.Config(**(values | overrides))  # type: ignore[arg-type]


def cold_refusal() -> dict[str, str]:
    """What the API sends for a keyless cold ticker: the anonymous plan's
    cold_ingests_per_day is 0 in production, so the first charge is refused."""
    now = datetime.now(timezone.utc)
    result = daily_result(False, 0, limit=0, day=now.date(), now=now)
    exc = RateLimited(Quota.COLD_INGESTS_PER_DAY.value, result)
    return ErrorOut(error="rate_limited", detail=str(exc)).model_dump()


def not_listed(symbol: str) -> dict[str, str]:
    return ErrorOut(error="not_found", detail=str(SymbolNotListed(symbol))).model_dump()


HEALTHY = {
    "status": "ok",
    "database": "ok",
    "redis": "ok",
    "news_provider": "exa",
    "news_provider_configured": True,
    "llm_provider": "anthropic",
    "llm_configured": True,
}

ARTICLE = {
    "article": {"id": 1, "url": "https://news.test/a", "title": "Earnings", "source": "news.test"},
    "relevance_tier": "easy",
    "relevance_score": 0.9,
}


def ticker_body(movements: list[dict], *, status: str = "ready", warnings: list[str] | None = None) -> dict:
    return {"status": status, "movements": movements, "warnings": warnings or [], "job_id": None}


WARM = ticker_body([{"id": 1, "news": [ARTICLE]}, {"id": 2, "news": []}])
CHAT = {
    "conversation_id": "c1",
    "ticker": "NVDA",
    "answer": "NVDA fell 8% on export restrictions [M1] [A1].",
    "sources": {"movements": [{"ref": "M1"}], "articles": [{"ref": "A1"}]},
    "grounded": True,
}


class FakeAPI:
    """A production API that behaves, with any route overridable, recording
    every request it gets."""

    def __init__(self, overrides: dict[str, Handler] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Handler] = {
            "GET /health": lambda r: httpx.Response(200, json=HEALTHY),
            "GET /tickers/NVDA": lambda r: httpx.Response(200, json=WARM),
            "GET /tickers/CLX": lambda r: httpx.Response(429, json=cold_refusal()),
            "GET /tickers/ZZZZQ": lambda r: httpx.Response(404, json=not_listed("ZZZZQ")),
            "POST /chat": lambda r: httpx.Response(200, json=CHAT),
            "OPTIONS /tickers/NVDA": self.cors,
        }
        self.routes.update(overrides or {})

    @staticmethod
    def cors(request: httpx.Request) -> httpx.Response:
        # Starlette's CORSMiddleware: allowed origins get the headers, others a
        # bare 400.
        origin = request.headers.get("origin")
        if origin != ORIGIN:
            return httpx.Response(400, text="Disallowed CORS origin")
        return httpx.Response(
            200,
            headers={
                "access-control-allow-origin": origin,
                "access-control-allow-headers": "Accept, Accept-Language, Authorization, Content-Language, Content-Type",
                "access-control-allow-methods": "GET, POST, OPTIONS",
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(f"{request.method} {request.url.path}")
        return handler(request) if handler else httpx.Response(404, json={"detail": "Not Found"})

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=API, transport=httpx.MockTransport(self))


def run_one(check: smoke.Check, api: FakeAPI | None = None, **overrides: object) -> smoke.Result:
    with (api or FakeAPI()).client() as http:
        return check(http, config(**overrides))


# ----------------------------------------------------------- defaults


def test_default_symbols_are_well_formed():
    defaults = smoke.Config(url=API)
    assert all(is_valid_symbol(s) for s in (defaults.warm, defaults.cold, defaults.fake))


def test_config_from_env():
    cfg = smoke.Config.from_env(
        {"METRIX_URL": f"{API}/", "METRIX_API_KEY": KEY, "METRIX_COLD_SYMBOL": "pep", "METRIX_FRONTEND_ORIGIN": ""}
    )
    assert (cfg.url, cfg.api_key, cfg.cold, cfg.warm, cfg.frontend_origin) == (API, KEY, "PEP", "NVDA", None)


def test_missing_url_exits_2(capsys):
    assert smoke.main([], env={}) == 2
    assert "METRIX_URL" in capsys.readouterr().err


# --------------------------------------------------------------- health


def test_health_passes():
    assert run_one(smoke.check_health).outcome is smoke.Outcome.PASS


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "degraded"),
        ("database", "unreachable"),
        ("redis", "unreachable"),
        ("redis", "not_configured"),
        ("news_provider_configured", False),
        ("llm_configured", False),
        ("news_provider", "fixture"),
    ],
)
def test_health_fails_on_any_bad_field(field, value):
    api = FakeAPI({"GET /health": lambda r: httpx.Response(200, json=HEALTHY | {field: value})})
    result = run_one(smoke.check_health, api)
    assert result.outcome is smoke.Outcome.FAIL
    assert field in result.detail


def test_health_fails_on_an_error_status():
    api = FakeAPI({"GET /health": lambda r: httpx.Response(502, text="Bad Gateway")})
    result = run_one(smoke.check_health, api)
    assert result.outcome is smoke.Outcome.FAIL
    assert "HTTP 502" in result.detail


# ---------------------------------------------------------- warm ticker


def test_warm_ticker_passes():
    result = run_one(smoke.check_warm_ticker)
    assert result.outcome is smoke.Outcome.PASS
    assert "2 movement(s), 1 with news" in result.detail


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=ticker_body([])),
        httpx.Response(200, json=ticker_body([{"id": 1, "news": []}])),
        httpx.Response(202, json=ticker_body([], status="ingesting")),
        httpx.Response(429, json=cold_refusal()),
    ],
    ids=["no movements", "no news", "ingesting", "refused"],
)
def test_warm_ticker_fails(response):
    api = FakeAPI({"GET /tickers/NVDA": lambda r: response})
    assert run_one(smoke.check_warm_ticker, api).outcome is smoke.Outcome.FAIL


# ---------------------------------------------------------- cold ticker


def test_cold_ticker_refused_by_the_cold_quota_passes():
    result = run_one(smoke.check_cold_ticker)
    assert result.outcome is smoke.Outcome.PASS
    assert "cold_ingests_per_day" in result.detail


def test_cold_ticker_with_stored_data_fails_with_a_hint():
    warning = f"Showing stored data; nothing new was fetched: {cold_refusal()['detail']}"
    body = ticker_body([{"id": 1, "news": [ARTICLE]}], warnings=[warning])
    api = FakeAPI({"GET /tickers/CLX": lambda r: httpx.Response(200, json=body)})
    result = run_one(smoke.check_cold_ticker, api)
    assert result.outcome is smoke.Outcome.FAIL
    assert "METRIX_COLD_SYMBOL" in result.detail


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(202, json=ticker_body([], status="ingesting") | {"job_id": 7}),
        httpx.Response(200, json=ticker_body([{"id": 1, "news": []}])),
        httpx.Response(429, json={"error": "rate_limited", "detail": "Over the requests_per_minute quota (30); retry in 2 s."}),
        httpx.Response(404, json=not_listed("CLX")),
    ],
    ids=["queued", "served", "other quota", "not listed"],
)
def test_cold_ticker_fails_unless_refused_by_the_cold_quota(response):
    api = FakeAPI({"GET /tickers/CLX": lambda r: response})
    assert run_one(smoke.check_cold_ticker, api).outcome is smoke.Outcome.FAIL


# ---------------------------------------------------------- fake ticker


def test_fake_ticker_not_listed_passes():
    result = run_one(smoke.check_fake_ticker)
    assert result.outcome is smoke.Outcome.PASS
    assert "not a listed US symbol" in result.detail


def test_fake_ticker_let_through_by_an_empty_directory_fails():
    api = FakeAPI({"GET /tickers/ZZZZQ": lambda r: httpx.Response(429, json=cold_refusal())})
    result = run_one(smoke.check_fake_ticker, api)
    assert result.outcome is smoke.Outcome.FAIL
    assert "directory" in result.detail


def test_fake_ticker_not_found_by_ingestion_fails():
    # A 404 from a failed price fetch means the directory did not stop it.
    body = {"error": "not_found", "detail": "No price data available for ticker 'ZZZZQ'."}
    api = FakeAPI({"GET /tickers/ZZZZQ": lambda r: httpx.Response(404, json=body)})
    assert run_one(smoke.check_fake_ticker, api).outcome is smoke.Outcome.FAIL


# ------------------------------------------------------------------ chat


def test_chat_passes_and_sends_the_key():
    api = FakeAPI()
    result = run_one(smoke.check_chat, api)
    assert result.outcome is smoke.Outcome.PASS
    (request,) = api.requests
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content)["ticker"] == "NVDA"


def test_chat_is_skipped_without_a_key():
    api = FakeAPI()
    result = run_one(smoke.check_chat, api, api_key=None)
    assert result.outcome is smoke.Outcome.SKIP
    assert api.requests == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=CHAT | {"answer": "  "}),
        httpx.Response(401, json={"error": "unauthorized", "detail": "Invalid API key."}),
        httpx.Response(503, json={"error": "spend_cap_reached", "detail": "Cap reached."}),
    ],
    ids=["empty answer", "bad key", "spend cap"],
)
def test_chat_fails(response):
    api = FakeAPI({"POST /chat": lambda r: response})
    assert run_one(smoke.check_chat, api).outcome is smoke.Outcome.FAIL


# ------------------------------------------------------------------ CORS


def test_cors_passes():
    assert run_one(smoke.check_cors_frontend).outcome is smoke.Outcome.PASS
    assert run_one(smoke.check_cors_stranger).outcome is smoke.Outcome.PASS


def test_cors_frontend_fails_when_not_allowed():
    result = run_one(smoke.check_cors_frontend, frontend_origin="https://other.vercel.app")
    assert result.outcome is smoke.Outcome.FAIL
    assert "CORS_ALLOWED_ORIGINS" in result.detail


def test_cors_stranger_fails_when_allowed():
    def allow_all(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"access-control-allow-origin": "*"})

    api = FakeAPI({"OPTIONS /tickers/NVDA": allow_all})
    assert run_one(smoke.check_cors_stranger, api).outcome is smoke.Outcome.FAIL


def test_cors_is_skipped_without_an_origin():
    api = FakeAPI()
    for check in (smoke.check_cors_frontend, smoke.check_cors_stranger):
        assert run_one(check, api, frontend_origin=None).outcome is smoke.Outcome.SKIP
    assert api.requests == []


# ------------------------------------------------------------ the whole run


def test_all_pass_exits_0():
    api = FakeAPI()
    env = {"METRIX_URL": API, "METRIX_API_KEY": KEY, "METRIX_FRONTEND_ORIGIN": ORIGIN}
    assert smoke.main([], env=env, transport=httpx.MockTransport(api)) == 0


def test_any_fail_exits_1(capsys):
    api = FakeAPI({"GET /health": lambda r: httpx.Response(200, json=HEALTHY | {"redis": "unreachable"})})
    env = {"METRIX_URL": API, "METRIX_API_KEY": KEY}
    assert smoke.main([], env=env, transport=httpx.MockTransport(api)) == 1
    out = capsys.readouterr().out
    assert "FAIL  health" in out
    assert "1 failed" in out


def test_a_network_error_fails_that_check_only():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    api = FakeAPI({"GET /health": down})
    with api.client() as http:
        results = smoke.run(config(), http, out=lambda line: None)
    assert [r.outcome for r in results] == [smoke.Outcome.FAIL] + [smoke.Outcome.PASS] * 6
    assert "ConnectError" in results[0].detail


def test_only_chat_is_paid_and_only_chat_sends_the_key():
    api = FakeAPI()
    with api.client() as http:
        results = smoke.run(config(), http, out=lambda line: None)
    assert all(r.outcome is smoke.Outcome.PASS for r in results)
    keyed = [f"{r.method} {r.url.path}" for r in api.requests if "authorization" in r.headers]
    assert keyed == ["POST /chat"]
    # Everything else is a GET or a preflight: nothing that could start paid work.
    others = {f"{r.method} {r.url.path}" for r in api.requests if "authorization" not in r.headers}
    assert others == {
        "GET /health",
        "GET /tickers/NVDA",
        "GET /tickers/CLX",
        "GET /tickers/ZZZZQ",
        "OPTIONS /tickers/NVDA",
    }


def test_the_key_never_appears_in_output(capsys):
    # Every check fails, and the failing bodies echo the key back: the worst case.
    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"boom {KEY} {request.headers.get('authorization')}")

    api = FakeAPI()
    api.routes = {route: echo for route in api.routes}
    env = {"METRIX_URL": API, "METRIX_API_KEY": KEY, "METRIX_FRONTEND_ORIGIN": ORIGIN}
    assert smoke.main([], env=env, transport=httpx.MockTransport(api)) == 1
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err
    assert "[redacted]" in captured.out
