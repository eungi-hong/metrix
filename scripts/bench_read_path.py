#!/usr/bin/env python3
"""Time the warm read path, GET /tickers/{symbol}, on a local synthetic database.

Phase 0 of v2 needs a "before" number for the endpoint Phase 4 will cache.
Production must not be load tested, and the numbers have to be reproducible
without yfinance, Exa or an LLM, so they come from a local API on local
Postgres and Redis, over data built here:

* prices are synthetic: a seeded random walk with occasional fat-tailed
  jumps, one per symbol, so two runs build the same rows and the detector
  finds a realistic number of movements;
* news comes from the fixture provider (``NEWS_PROVIDER=fixture``), through
  the real response cache;
* scoring is a deterministic stand-in that links three candidates per
  movement, so each movement carries evidence like a production one.

The data goes through the real pipeline (``ingest_ticker``), so the rows the
read path touches (bars, movements, links, articles) have production's shape.
What the numbers measure is the API's own cost of a warm read on this
machine. They say nothing about production's network, hardware or row counts;
label them "synthetic-local" wherever they are quoted.

Steps (Postgres on 5433 and Redis on 6380, as `docker compose up -d db redis`
gives you)::

    # 1. A fresh, migrated, throwaway database. Its name must contain "bench"
    #    or "test". Reseed into a fresh one: cached searches replay.
    createdb -h 127.0.0.1 -p 5433 -U metrix metrix_bench
    export DATABASE_URL=postgresql+asyncpg://metrix:metrix@127.0.0.1:5433/metrix_bench
    alembic upgrade head
    python scripts/bench_read_path.py seed

    # 2. The API, in another terminal, started the way production starts it
    #    (one uvicorn worker, no reload), with this environment:
    DATABASE_URL=postgresql+asyncpg://metrix:metrix@127.0.0.1:5433/metrix_bench \\
    REDIS_URL=redis://127.0.0.1:6380/0 \\
    NEWS_PROVIDER=fixture \\
    AUTH_REQUIRED=false \\
    PLAN_LIMITS_JSON='{"anonymous": {"requests_per_minute": 1000000}}' \\
    uvicorn app.main:app --host 127.0.0.1 --port 8000

    # 3. Within STALENESS_HOURS (24) of the seed, or the ticker is stale:
    python scripts/bench_read_path.py run --base-url http://127.0.0.1:8000 \\
        --json-out docs/v2/baseline/read_path_synthetic_local.json

Why that environment: requests go out without a key, as the v1 frontend's
do. `AUTH_REQUIRED=false` is what production and .env.example set (the code
default is true, which would answer 401), and it serves them as the
anonymous plan, which allows 30 requests a minute. Raising that one number
keeps every request on production's code path (the quota is still charged
in Redis on each call) without turning most of the run into 429s.
`NEWS_PROVIDER=fixture` means no Exa key is needed; the read path makes no
news or LLM call either way.

``run`` refuses to time anything unless the first response is a warm
``ready`` with no job and no warning, so it cannot silently measure an
ingestion, a quota refusal or a cold path instead, and it exits non-zero
without writing results if any timed request failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

T = TypeVar("T", bound=BaseModel)

# The five tickers production pre-warms every night: the README's live-demo
# section, and PREWARM_SEED_SYMBOLS in .env.production.example. (The local
# default, data/seed_universe.txt, lists 40.)
SEED_SYMBOLS: tuple[str, ...] = ("NVDA", "AAPL", "TSLA", "MSFT", "AMZN")

# Sector and industry per synthetic series, in yfinance's style. Only the
# Hard-tier cache key reads them here; they are labels, not data.
_PROFILES: dict[str, tuple[str, str]] = {
    "NVDA": ("Technology", "Semiconductors"),
    "AAPL": ("Technology", "Consumer Electronics"),
    "TSLA": ("Consumer Cyclical", "Auto Manufacturers"),
    "MSFT": ("Technology", "Software - Infrastructure"),
    "AMZN": ("Consumer Cyclical", "Internet Retail"),
}
_DEFAULT_PROFILE = ("Technology", "Software - Application")

# Daily volatility of each synthetic series. An assumption chosen to span a
# quiet and a volatile name, not a measurement of the real stocks.
SYNTHETIC_DAILY_VOL: dict[str, float] = {
    "NVDA": 0.030,
    "AAPL": 0.016,
    "TSLA": 0.038,
    "MSFT": 0.014,
    "AMZN": 0.020,
}
DEFAULT_DAILY_VOL = 0.02
# Fat tails: on this share of days the shock is scaled up, as real returns are.
JUMP_PROBABILITY = 0.05
JUMP_SCALE = 3.0
# Share of each day's shock that shows up as the opening gap.
GAP_SHARE = 0.4
BASE_PRICE = 100.0
BASE_VOLUME = 30_000_000
VOLUME_LOG_SIGMA = 0.3
PRICE_DIGITS = 4

# A fixed end keeps the rows identical between runs. Its news windows have
# long closed, so every movement is enriched COMPLETE, as a warm one is.
DEFAULT_END = date(2026, 10, 9)
# Calendar days of history, as PRICE_HISTORY_DAYS's default fetches.
HISTORY_CALENDAR_DAYS = 365

# Enough for every movement in one pass: a warm production ticker has its
# movements enriched by the nightly budget, so none is left owed.
SEED_MAX_MOVEMENTS_PER_INGEST = 10_000

# The stand-in scorer's verdicts, by candidate position: (tier, score).
# Anything past these is irrelevant, as most candidates are in production.
_STAND_IN_VERDICTS: tuple[tuple[str, float], ...] = (
    ("easy", 0.91),
    ("hard", 0.62),
    ("medium", 0.48),
)

# Query shapes worth timing: the API's default, and what the v1 frontend asks.
SHAPES: dict[str, dict[str, str]] = {
    "default": {},
    "frontend": {"include_prices": "true", "limit": "50"},
}
DEFAULT_REQUESTS = 500
DEFAULT_WARMUP = 50
HTTP_TIMEOUT_SECONDS = 30.0
PERCENTILE_CUTS = 100

# The label every number this script produces must carry when quoted.
RESULT_LABEL = "synthetic-local"
REPO_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------- seeding


def synthetic_history(
    symbol: str, end: date = DEFAULT_END, calendar_days: int = HISTORY_CALENDAR_DAYS
) -> Any:
    """A deterministic year of weekday bars for `symbol`. Holidays are ignored."""
    from app.services.prices import PriceBarData, PriceHistory, TickerProfile

    rng = random.Random(f"metrix-bench:{symbol}")
    vol = SYNTHETIC_DAILY_VOL.get(symbol, DEFAULT_DAILY_VOL)
    close = BASE_PRICE
    bars: list[PriceBarData] = []
    day = end - timedelta(days=calendar_days)
    while day <= end:
        if day.weekday() < 5:
            shock = rng.gauss(0.0, vol)
            if rng.random() < JUMP_PROBABILITY:
                shock *= JUMP_SCALE
            opened = close * (1 + shock * GAP_SHARE)
            close = close * (1 + shock)
            wick = abs(rng.gauss(0.0, vol / 2))
            bars.append(
                PriceBarData(
                    date=day,
                    open=round(opened, PRICE_DIGITS),
                    high=round(max(opened, close) * (1 + wick), PRICE_DIGITS),
                    low=round(min(opened, close) * (1 - wick), PRICE_DIGITS),
                    close=round(close, PRICE_DIGITS),
                    adj_close=round(close, PRICE_DIGITS),
                    volume=int(BASE_VOLUME * math.exp(rng.gauss(0.0, VOLUME_LOG_SIGMA))),
                )
            )
        day += timedelta(days=1)

    sector, industry = _PROFILES.get(symbol, _DEFAULT_PROFILE)
    return PriceHistory(
        profile=TickerProfile(
            symbol=symbol,
            company_name=f"{symbol} (synthetic)",
            sector=sector,
            industry=industry,
            exchange="NASDAQ",
            currency="USD",
        ),
        bars=bars,
    )


def _stand_in_scorer() -> Any:
    """A deterministic LLMProvider for seeding: no network, no ledger."""
    from app.services.llm import LLMProvider
    from app.services.peers import PeerSet
    from app.services.relevance import ArticleAssessment, RelevanceReport

    class StandInScorer(LLMProvider):
        name = "bench-stand-in"
        model = "bench-stand-in"

        @property
        def available(self) -> bool:
            return True

        async def parse_structured(
            self,
            *,
            system: str,
            user: str,
            output_model: type[T],
            operation: str,
            max_tokens: int | None = None,
        ) -> T:
            if output_model is PeerSet:
                return PeerSet(  # type: ignore[return-value]
                    peers=[{"name": "Synthetic Rival", "ticker": "RVL", "relationship": "competitor"}],
                    industry_themes=["synthetic demand"],
                )
            if output_model is RelevanceReport:
                count = user.count("] title:")
                assessments = []
                for index in range(count):
                    tier, score = (
                        _STAND_IN_VERDICTS[index]
                        if index < len(_STAND_IN_VERDICTS)
                        else ("irrelevant", 0.05)
                    )
                    assessments.append(
                        ArticleAssessment(
                            index=index,
                            tier=tier,  # type: ignore[arg-type]
                            score=score,
                            rationale="Stand-in verdict for the synthetic benchmark.",
                        )
                    )
                return RelevanceReport(assessments=assessments)  # type: ignore[return-value]
            raise TypeError(f"The stand-in scorer has no answer for {output_model.__name__}")

        async def complete(self, *, system: str, messages: list[Any], operation: str, max_tokens: int | None = None) -> str:
            raise TypeError("The stand-in scorer does not answer chat")

    return StandInScorer()


def _windowed_fixture_provider() -> Any:
    """The fixture provider, with each search window's articles at their own URLs.

    `FixtureNewsProvider` seeds its URLs on the query text alone, and a tier
    query names no date, so every movement of a ticker gets the same URLs.
    Deduplication then resolves them to the first movement's articles, whose
    dates fall outside every later window, and only one movement per ticker
    ends up with links. Folding the window start into the seed gives each
    movement articles of its own, as a real search does. The cache key is
    still taken from the unmodified request.
    """
    from dataclasses import replace

    from app.services.news.base import NewsSearchRequest
    from app.services.news.fixture import FixtureNewsProvider

    class WindowedFixtureProvider(FixtureNewsProvider):
        async def execute(self, request: NewsSearchRequest) -> dict[str, Any]:
            dated = replace(request, query=f"{request.query} @{request.start.date().isoformat()}")
            return await super().execute(dated)

    return WindowedFixtureProvider()


def _require_throwaway_database(url: str) -> None:
    from sqlalchemy.engine import make_url

    name = make_url(url).database or ""
    if "bench" not in name and "test" not in name:
        raise SystemExit(
            f"Refusing to seed '{name}': the database name must contain 'bench' or 'test', "
            "so a real database is never filled with synthetic rows."
        )


async def seed(symbols: list[str], end: date) -> dict[str, int]:
    """Ingest every symbol through the real pipeline. Returns row counts."""
    import sqlalchemy as sa

    from app.core.config import settings
    from app.db.session import SessionLocal, dispose_engine
    from app.models.market import Movement, PriceBar, Ticker
    from app.models.news import MovementNewsLink, NewsArticle, NewsQueryCache
    from app.services import ingestion
    from app.services import prices as price_service
    from app.services.news import CachingNewsProvider

    _require_throwaway_database(settings.database_url)
    settings.news_provider = "fixture"
    settings.max_movements_per_ingest = SEED_MAX_MOVEMENTS_PER_INGEST

    async def synthetic_fetch(symbol: str, days: int | None = None) -> Any:
        return synthetic_history(symbol, end)

    price_service.fetch_price_history = synthetic_fetch  # type: ignore[assignment]
    scorer = _stand_in_scorer()

    try:
        for symbol in symbols:
            async with SessionLocal() as session:
                ticker = await ingestion.get_or_create_ticker(session, symbol)
                await session.commit()
                if not await ingestion.claim_ingestion(session, ticker):
                    raise SystemExit(f"{symbol} is claimed by another ingestion")
                result = await ingestion.ingest_ticker(
                    session,
                    symbol,
                    llm=scorer,
                    news_provider=CachingNewsProvider(_windowed_fixture_provider(), session),
                    allow_followup_jobs=False,
                )
                print(
                    f"{symbol}: {result.bars_written} bars written, "
                    f"{result.movements_detected} movements, "
                    f"{result.articles_linked} links"
                    + (f", warnings: {result.warnings}" if result.warnings else "")
                )

        async with SessionLocal() as session:
            counts: dict[str, int] = {}
            for label, model in (
                ("tickers", Ticker),
                ("price_bars", PriceBar),
                ("movements", Movement),
                ("links", MovementNewsLink),
                ("articles", NewsArticle),
                ("news_cache_rows", NewsQueryCache),
            ):
                counts[label] = int(
                    await session.scalar(sa.select(sa.func.count()).select_from(model)) or 0
                )
            return counts
    finally:
        await dispose_engine()


# ------------------------------------------------------------------- timing


@dataclass(frozen=True, slots=True)
class Summary:
    """One timed series of requests."""

    shape: str
    concurrency: int
    requests: int
    errors: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    max_ms: float
    throughput_rps: float
    response_bytes: int


def summarize(
    shape: str,
    concurrency: int,
    latencies_s: list[float],
    errors: int,
    wall_s: float,
    response_bytes: int,
) -> Summary:
    """Percentiles in milliseconds. Inclusive quantiles, so p100 is the max."""
    if not latencies_s:
        raise ValueError("no successful requests to summarise")
    ms = sorted(value * 1000 for value in latencies_s)
    cuts = (
        statistics.quantiles(ms, n=PERCENTILE_CUTS, method="inclusive")
        if len(ms) > 1
        else [ms[0]] * (PERCENTILE_CUTS - 1)
    )
    return Summary(
        shape=shape,
        concurrency=concurrency,
        requests=len(ms) + errors,
        errors=errors,
        p50_ms=round(statistics.median(ms), 2),
        p95_ms=round(cuts[94], 2),
        p99_ms=round(cuts[98], 2),
        mean_ms=round(statistics.fmean(ms), 2),
        max_ms=round(ms[-1], 2),
        throughput_rps=round((len(ms) + errors) / wall_s, 1) if wall_s > 0 else 0.0,
        response_bytes=response_bytes,
    )


@dataclass(frozen=True, slots=True)
class Payload:
    """What one timed response contained, so readers can compare its size."""

    shape: str
    params: dict[str, str]
    status_code: int
    body_bytes: int
    movements_total: int
    movements_returned: int
    links_returned: int
    price_bars_returned: int
    # The quota in force for these requests, as the API reported it.
    ratelimit_limit: str | None


def describe_payload(
    shape: str, params: dict[str, str], status_code: int, body: bytes, headers: Any
) -> Payload:
    payload = json.loads(body)
    movements = payload.get("movements") or []
    return Payload(
        shape=shape,
        params=dict(params),
        status_code=status_code,
        body_bytes=len(body),
        movements_total=int((payload.get("pagination") or {}).get("total") or 0),
        movements_returned=len(movements),
        links_returned=sum(len(m.get("news") or []) for m in movements),
        price_bars_returned=len(payload.get("prices") or []),
        ratelimit_limit=headers.get("x-ratelimit-limit"),
    )


def check_warm(payload: dict[str, Any]) -> list[str]:
    """Why a response is not a warm read, or an empty list if it is."""
    problems = []
    if payload.get("status") != "ready":
        problems.append(f"status is {payload.get('status')!r}, not 'ready'")
    if payload.get("job_id") is not None:
        problems.append(f"a job was queued ({payload.get('job_id')})")
    if payload.get("warnings"):
        problems.append(f"warnings: {payload.get('warnings')}")
    if not payload.get("movements"):
        problems.append("no movements returned")
    return problems


async def time_shape(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, str],
    *,
    shape: str,
    requests: int,
    warmup: int,
    concurrency: int,
) -> tuple[Summary, Payload]:
    first = await client.get(path, params=params)
    first.raise_for_status()
    problems = check_warm(first.json())
    if problems:
        raise SystemExit(f"{shape}: not a warm read ({'; '.join(problems)}). Re-run `seed`.")
    response_bytes = len(first.content)
    payload = describe_payload(shape, params, first.status_code, first.content, first.headers)

    for _ in range(warmup):
        (await client.get(path, params=params)).raise_for_status()

    latencies: list[float] = []
    errors = 0
    per_worker = [requests // concurrency + (1 if i < requests % concurrency else 0) for i in range(concurrency)]

    async def worker(count: int) -> None:
        nonlocal errors
        for _ in range(count):
            started = time.perf_counter()
            response = await client.get(path, params=params)
            elapsed = time.perf_counter() - started
            if response.status_code == 200:
                latencies.append(elapsed)
            else:
                errors += 1

    started = time.perf_counter()
    await asyncio.gather(*(worker(count) for count in per_worker))
    wall = time.perf_counter() - started
    return summarize(shape, concurrency, latencies, errors, wall, response_bytes), payload


async def run(
    base_url: str,
    symbol: str,
    shapes: list[str],
    concurrencies: list[int],
    requests: int,
    warmup: int,
) -> tuple[list[Summary], dict[str, Payload]]:
    limits = httpx.Limits(max_connections=max(concurrencies), max_keepalive_connections=max(concurrencies))
    async with httpx.AsyncClient(base_url=base_url, timeout=HTTP_TIMEOUT_SECONDS, limits=limits) as client:
        results: list[Summary] = []
        payloads: dict[str, Payload] = {}
        for shape in shapes:
            for concurrency in concurrencies:
                summary, payload = await time_shape(
                    client,
                    f"/tickers/{symbol}",
                    SHAPES[shape],
                    shape=shape,
                    requests=requests,
                    warmup=warmup,
                    concurrency=concurrency,
                )
                results.append(summary)
                payloads.setdefault(shape, payload)
        return results, payloads


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _cpu_model() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def provenance(base_url: str, symbol: str, requests: int, warmup: int) -> dict[str, Any]:
    """Where and how the numbers were taken, stored next to them."""
    return {
        "label": RESULT_LABEL,
        "what": "GET /tickers/{symbol} on a warm, synthetic, local database; "
        "client and server on the same machine",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
        "command": " ".join(["python", "scripts/bench_read_path.py", *sys.argv[1:]]),
        "server_environment": "docs/v2/BASELINE.md, and this script's docstring",
        "base_url": base_url,
        "symbol": symbol,
        "requests_per_series": requests,
        "warmup_requests_per_series": warmup,
        "python": platform.python_version(),
        "httpx": httpx.__version__,
        "os": platform.platform(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count(),
    }


def markdown_table(results: list[Summary]) -> str:
    header = (
        "| Query | Concurrency | Requests | Errors | p50 ms | p95 ms | p99 ms | "
        "Mean ms | Max ms | Req/s | Body bytes |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    rows = [
        f"| {r.shape} | {r.concurrency} | {r.requests} | {r.errors} | {r.p50_ms} | "
        f"{r.p95_ms} | {r.p99_ms} | {r.mean_ms} | {r.max_ms} | {r.throughput_rps} | "
        f"{r.response_bytes} |"
        for r in results
    ]
    return "\n".join([header, *rows])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    seed_cmd = sub.add_parser("seed", help="fill DATABASE_URL with synthetic, enriched tickers")
    seed_cmd.add_argument("--symbols", default=",".join(SEED_SYMBOLS))
    seed_cmd.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)

    run_cmd = sub.add_parser("run", help="time GET /tickers/{symbol} against a running API")
    run_cmd.add_argument("--base-url", default="http://127.0.0.1:8000")
    run_cmd.add_argument("--symbol", default="NVDA")
    run_cmd.add_argument("--shape", action="append", choices=sorted(SHAPES))
    run_cmd.add_argument("--concurrency", action="append", type=int)
    run_cmd.add_argument("--requests", type=int, default=DEFAULT_REQUESTS)
    run_cmd.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    run_cmd.add_argument("--json-out", type=Path)

    args = parser.parse_args()
    if args.command == "seed":
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        counts = asyncio.run(seed(symbols, args.end))
        print(json.dumps(counts, indent=2))
        return

    symbol = args.symbol.upper()
    results, payloads = asyncio.run(
        run(
            args.base_url,
            symbol,
            args.shape or ["default", "frontend"],
            args.concurrency or [1, 8],
            args.requests,
            args.warmup,
        )
    )
    print(markdown_table(results))
    failed = [r for r in results if r.errors]
    if failed:
        # A refused or failed request is fast, and would flatter the percentiles.
        raise SystemExit(
            "Some timed requests failed; nothing was written: "
            + ", ".join(f"{r.shape} x{r.concurrency}: {r.errors}" for r in failed)
        )
    if args.json_out:
        document = {
            "provenance": provenance(args.base_url, symbol, args.requests, args.warmup),
            "payloads": [asdict(p) for p in payloads.values()],
            "results": [asdict(r) for r in results],
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(document, indent=2) + "\n")


if __name__ == "__main__":
    main()
