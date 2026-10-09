#!/usr/bin/env python3
"""Smoke-test a deployed Metrix API: is production actually serving?

CI proves the code works against service containers; this proves the
deployment does: the database, Redis and both provider keys are wired up, a
pre-warmed ticker has movements with news, the anonymous plan refuses paid
work, the symbol directory refuses unknown symbols, chat answers, and CORS
admits the frontend and nobody else.

    METRIX_URL=https://api.example.up.railway.app python scripts/smoke.py

Read from the environment:

    METRIX_URL              the API's base URL (required)
    METRIX_API_KEY          the demo key; without it the chat check is skipped
    METRIX_FRONTEND_ORIGIN  e.g. https://metrix.vercel.app; without it the CORS
                            checks are skipped
    METRIX_WARM_SYMBOL      a pre-warmed ticker (default NVDA)
    METRIX_COLD_SYMBOL      a real US listing with nothing stored (default CLX)
    METRIX_FAKE_SYMBOL      well-formed but unlisted (default ZZZZQ)

Every check but chat is keyless and costs nothing: the cold ticker is refused
by the anonymous quota before any provider is called, and the fake one by the
symbol directory before that. Chat is one paid LLM call, the only one, and
the only request that carries the key. The key is never printed.

Prints one PASS, FAIL or SKIP line per check. Exits 1 if any check failed,
2 if METRIX_URL is not set.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

DEFAULT_TIMEOUT = 30.0
# A chat turn is a retrieval plus one LLM call; give it room.
CHAT_TIMEOUT = 90.0
EVIL_ORIGIN = "https://evil.example"
COLD_QUOTA = "cold_ingests_per_day"
BODY_PREVIEW = 300
DEFAULT_WARM = "NVDA"  # pre-warmed every weekday night
DEFAULT_COLD = "CLX"  # NYSE-listed, and in neither seed list (data/seed_universe.txt has KO)
DEFAULT_FAKE = "ZZZZQ"  # passes the symbol regex, listed nowhere


class ConfigError(ValueError):
    """The environment does not say what to test."""


@dataclass(frozen=True, slots=True)
class Config:
    url: str
    api_key: str | None = None
    frontend_origin: str | None = None
    warm: str = DEFAULT_WARM
    cold: str = DEFAULT_COLD
    fake: str = DEFAULT_FAKE

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        def get(name: str) -> str | None:
            value = env.get(name, "").strip()
            return value or None

        url = get("METRIX_URL")
        if url is None:
            raise ConfigError("METRIX_URL is not set: the API's base URL, e.g. https://api.example.com")
        return cls(
            url=url.rstrip("/"),
            api_key=get("METRIX_API_KEY"),
            frontend_origin=(get("METRIX_FRONTEND_ORIGIN") or "").rstrip("/") or None,
            warm=(get("METRIX_WARM_SYMBOL") or DEFAULT_WARM).upper(),
            cold=(get("METRIX_COLD_SYMBOL") or DEFAULT_COLD).upper(),
            fake=(get("METRIX_FAKE_SYMBOL") or DEFAULT_FAKE).upper(),
        )


class Outcome(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    outcome: Outcome
    detail: str


def passed(name: str, detail: str) -> Result:
    return Result(name, Outcome.PASS, detail)


def failed(name: str, detail: str) -> Result:
    return Result(name, Outcome.FAIL, detail)


def skipped(name: str, detail: str) -> Result:
    return Result(name, Outcome.SKIP, detail)


def describe(response: httpx.Response) -> str:
    """Status and the start of the body, for a FAIL line."""
    body = " ".join(response.text.split())
    if len(body) > BODY_PREVIEW:
        body = body[:BODY_PREVIEW] + "..."
    return f"HTTP {response.status_code}: {body or '(empty body)'}"


def json_body(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------- checks
#
# Each takes a client whose base_url is the API and returns one Result. None
# of them sets the Authorization header except `check_chat`.


def check_health(http: httpx.Client, config: Config) -> Result:
    name = "health"
    response = http.get("/health")
    body = json_body(response)
    if response.status_code != 200 or not body:
        return failed(name, describe(response))
    # /health answers 200 even when degraded, so the body is what counts.
    expected: dict[str, object] = {
        "status": "ok",
        "database": "ok",
        "redis": "ok",
        "news_provider_configured": True,
        "llm_configured": True,
    }
    wrong = [f"{key}={body.get(key)!r}" for key, value in expected.items() if body.get(key) != value]
    # `fixture` counts as configured, but serves synthetic articles.
    if body.get("news_provider") == "fixture":
        wrong.append("news_provider='fixture'")
    if wrong:
        return failed(name, "expected all 'ok' and both providers configured; got " + ", ".join(wrong))
    return passed(
        name,
        f"database ok, redis ok, news {body.get('news_provider')}, llm {body.get('llm_provider')}",
    )


def check_warm_ticker(http: httpx.Client, config: Config) -> Result:
    name = f"warm ticker {config.warm}"
    response = http.get(f"/tickers/{config.warm}")
    body = json_body(response)
    if response.status_code != 200:
        return failed(name, describe(response))
    movements = body.get("movements")
    if not isinstance(movements, list) or not movements:
        return failed(
            name,
            f"no movements (status {body.get('status')!r}); is {config.warm} pre-warmed? "
            "Set METRIX_WARM_SYMBOL to a seed symbol.",
        )
    with_news = [m for m in movements if isinstance(m, dict) and m.get("news")]
    if not with_news:
        return failed(name, f"{len(movements)} movement(s), none with linked articles")
    articles = sum(len(m["news"]) for m in with_news)
    detail = (
        f"{len(movements)} movement(s), {len(with_news)} with news "
        f"({articles} article(s)), status {body.get('status')!r}"
    )
    warnings = body.get("warnings") or []
    if warnings:
        detail += f", {len(warnings)} warning(s): {warnings[0]}"
    return passed(name, detail)


def check_cold_ticker(http: httpx.Client, config: Config) -> Result:
    """Keyless, a ticker with nothing stored needs new ingestion, which the
    anonymous plan's cold_ingests_per_day (0 in production) refuses with a
    429 before any provider is called."""
    name = f"cold ticker {config.cold} refused keyless"
    response = http.get(f"/tickers/{config.cold}")
    body = json_body(response)
    detail = str(body.get("detail", ""))
    if response.status_code == 429 and body.get("error") == "rate_limited" and COLD_QUOTA in detail:
        return passed(name, f"429 {detail}")
    hint = "Choose another METRIX_COLD_SYMBOL: a US listing nobody has fetched."
    if response.status_code == 200:
        warned = any(COLD_QUOTA in str(w) for w in body.get("warnings") or [])
        if warned:
            return failed(
                name,
                f"{config.cold} has stored data, so it was served with a warning instead of refused. {hint}",
            )
        return failed(
            name,
            f"{config.cold} was served (status {body.get('status')!r}) with no refusal: it is "
            f"warm, or the anonymous plan allows cold ingests (PLAN_LIMITS_JSON). {hint}",
        )
    if response.status_code == 202:
        return failed(
            name,
            f"an ingestion was queued or joined keylessly (job {body.get('job_id')}): either "
            f"the anonymous plan allows cold ingests (PLAN_LIMITS_JSON), or a job for "
            f"{config.cold} was already queued, which is free to join. {hint}",
        )
    if response.status_code == 429:
        return failed(name, f"429, but not from {COLD_QUOTA}: {describe(response)}")
    if response.status_code == 404:
        return failed(name, f"the symbol directory refused it. {hint} {describe(response)}")
    return failed(name, describe(response))


def check_fake_ticker(http: httpx.Client, config: Config) -> Result:
    """Keyless: the directory check runs before any quota, so an unlisted
    symbol is a 404 that costs nothing and records no demand."""
    name = f"fake ticker {config.fake} not listed"
    response = http.get(f"/tickers/{config.fake}")
    body = json_body(response)
    detail = str(body.get("detail", ""))
    if response.status_code == 404 and body.get("error") == "not_found" and "not a listed US symbol" in detail:
        return passed(name, f"404 {detail}")
    if response.status_code == 429 and COLD_QUOTA in detail:
        return failed(
            name,
            "the symbol directory let it through and only the quota stopped it: the "
            "directory is empty (enforce acts as warn until the first weekly refresh) or "
            "SYMBOL_DIRECTORY_MODE is not enforce",
        )
    if response.status_code == 422:
        return failed(name, f"not a well-formed symbol; set METRIX_FAKE_SYMBOL. {describe(response)}")
    return failed(name, describe(response))


def check_chat(http: httpx.Client, config: Config) -> Result:
    name = f"chat about {config.warm}"
    if config.api_key is None:
        return skipped(name, "METRIX_API_KEY is not set")
    response = http.post(
        "/chat",
        json={
            "question": f"What was the largest recent move in {config.warm}, and why?",
            "ticker": config.warm,
        },
        headers={"Authorization": f"Bearer {config.api_key}"},
        timeout=CHAT_TIMEOUT,
    )
    body = json_body(response)
    if response.status_code != 200:
        return failed(name, describe(response))
    answer = body.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return failed(name, f"200 with an empty answer: {describe(response)}")
    sources = body.get("sources") or {}
    return passed(
        name,
        f"{len(answer)} chars, grounded={body.get('grounded')}, "
        f"{len(sources.get('movements') or [])} movement(s) and "
        f"{len(sources.get('articles') or [])} article(s) cited",
    )


def preflight(http: httpx.Client, path: str, origin: str) -> httpx.Response:
    return http.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )


def check_cors_frontend(http: httpx.Client, config: Config) -> Result:
    name = "CORS admits the frontend"
    if config.frontend_origin is None:
        return skipped(name, "METRIX_FRONTEND_ORIGIN is not set")
    response = preflight(http, f"/tickers/{config.warm}", config.frontend_origin)
    allowed = response.headers.get("access-control-allow-origin")
    if allowed != config.frontend_origin:
        return failed(
            name,
            f"access-control-allow-origin is {allowed!r}, expected {config.frontend_origin!r} "
            f"(is it in CORS_ALLOWED_ORIGINS?); HTTP {response.status_code}",
        )
    headers = response.headers.get("access-control-allow-headers", "").lower()
    if "authorization" not in headers:
        return failed(name, f"authorization is not an allowed header: {headers!r}")
    return passed(name, f"{config.frontend_origin} allowed, with the authorization header")


def check_cors_stranger(http: httpx.Client, config: Config) -> Result:
    name = "CORS refuses other origins"
    if config.frontend_origin is None:
        return skipped(name, "METRIX_FRONTEND_ORIGIN is not set")
    response = preflight(http, f"/tickers/{config.warm}", EVIL_ORIGIN)
    allowed = response.headers.get("access-control-allow-origin")
    if allowed is not None:
        return failed(name, f"{EVIL_ORIGIN} got access-control-allow-origin {allowed!r}")
    return passed(name, f"{EVIL_ORIGIN} got no access-control-allow-origin")


Check = Callable[[httpx.Client, Config], Result]

CHECKS: list[Check] = [
    check_health,
    check_warm_ticker,
    check_cold_ticker,
    check_fake_ticker,
    check_chat,
    check_cors_frontend,
    check_cors_stranger,
]


def run_check(check: Check, http: httpx.Client, config: Config) -> Result:
    """One check; a network error is that check's FAIL, not the run's end."""
    try:
        return check(http, config)
    except httpx.HTTPError as exc:
        name = check.__name__.removeprefix("check_").replace("_", " ")
        return failed(name, f"{type(exc).__name__}: {exc}")


def redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "[redacted]") if secret else text


def run(config: Config, http: httpx.Client, out: Callable[[str], None] = print) -> list[Result]:
    results = []
    for check in CHECKS:
        result = run_check(check, http, config)
        results.append(result)
        out(redact(f"{result.outcome.value}  {result.name}: {result.detail}", config.api_key))
    return results


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """`env` and `transport` are for tests: the process environment and the network otherwise."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="Configured by environment variables; see the module docstring.",
    )
    parser.parse_args(argv)
    try:
        config = Config.from_env(os.environ if env is None else env)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Smoke-testing {config.url}", flush=True)
    with httpx.Client(base_url=config.url, timeout=DEFAULT_TIMEOUT, transport=transport) as http:
        results = run(config, http)
    counts = {outcome: sum(r.outcome is outcome for r in results) for outcome in Outcome}
    print(
        f"{counts[Outcome.PASS]} passed, {counts[Outcome.FAIL]} failed, "
        f"{counts[Outcome.SKIP]} skipped"
    )
    return 1 if counts[Outcome.FAIL] else 0


if __name__ == "__main__":
    raise SystemExit(main())
