#!/usr/bin/env python3
"""Baseline numbers that only a real deployment can give, read over HTTP.

Phase 0 of v2 records "before" numbers. Read-path latency can be measured on
synthetic data (scripts/bench_read_path.py); these three cannot, because they
are about real providers and real stored data:

    movements   movements per ticker for the pre-warmed seed list, and how
                many of them have evidence (news_status, links per movement)
    usage       average spend per enrichment, from the spend ledger behind
                GET /admin/usage, summed over a range of UTC days
    chat        latency of three fixed questions, and how the answers cite:
                labels cited in the text against sources returned

Every subcommand only reads, except that each chat question is one model
call. Each writes a JSON document with where and when it was taken
(`--json-out`) and prints a summary.

    # 1. No key: production serves anonymous callers stored data and starts
    #    no new work for them (PLAN_LIMITS_JSON gives the anonymous plan
    #    cold_ingests_per_day=0), so this cannot queue paid enrichment. It
    #    does count as one anonymous request per symbol in demand ranking.
    python scripts/baseline_remote.py movements --base-url https://<api> \\
        --json-out movements.json

    # 2. Needs the admin token, read from METRIX_ADMIN_TOKEN, never printed.
    METRIX_ADMIN_TOKEN=... python scripts/baseline_remote.py usage \\
        --base-url https://<api> --days 14 --json-out usage.json

    # 3. Three model calls. Locally (needs keys in .env and NVDA ingested) or
    #    against the deployment; say which with --label.
    python scripts/baseline_remote.py chat --base-url http://localhost:8000 \\
        --label local --json-out chat.json

`movements` deliberately has no option to send an API key: a key on a plan
that may start work would turn this read into queued, paid enrichment for
every movement that is still owed news.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

# What production pre-warms every night (PREWARM_SEED_SYMBOLS in
# .env.production.example, and the README's live-demo section).
PRODUCTION_SEED_SYMBOLS: tuple[str, ...] = ("NVDA", "AAPL", "TSLA", "MSFT", "AMZN")

# The API's largest page; `movements` pages through anything longer.
PAGE_SIZE = 200
HTTP_TIMEOUT_SECONDS = 60.0
DEFAULT_USAGE_DAYS = 14

# The ledger's names for the calls an enrichment makes (app/services/news/exa.py,
# app/services/relevance.py, app/services/peers.py) and for a chat turn.
EXA_SEARCH = ("exa", "search")
OPERATION_RELEVANCE = "relevance"
OPERATION_PEERS = "peers"
OPERATION_CHAT = "chat"

# Fixed, so Phase 5 re-asks exactly these. The first is the everyday question,
# the second invites a list or table (markdown rendering), the third needs
# retrieval beyond the most recent movements (keyword search over evidence).
CHAT_TICKER = "NVDA"
CHAT_QUESTIONS: tuple[str, ...] = (
    "What drove NVDA's largest drop in the last year?",
    "List NVDA's three biggest up days and what explained each one.",
    "Has NVDA ever dropped on export-control news?",
)
# A 429 on chat is the per-minute quota; wait it out once rather than fail.
MAX_RETRY_AFTER_SECONDS = 120.0

CITATION = re.compile(r"\[([MA]\d+)\]")
MARKDOWN_MARKERS: dict[str, re.Pattern[str]] = {
    "heading": re.compile(r"^\s{0,3}#{1,6}\s", re.MULTILINE),
    "bold": re.compile(r"\*\*[^*\n]+\*\*"),
    "table": re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE),
    "bullet": re.compile(r"^\s*(?:[-*]|\d+\.)\s", re.MULTILINE),
}


def provenance(command: str, base_url: str, label: str | None = None) -> dict[str, Any]:
    return {
        "command": command,
        "label": label,
        "base_url": base_url,
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "script": "scripts/baseline_remote.py",
    }


def write_json(path: Path | None, document: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, default=str) + "\n")
    print(f"wrote {path}")


# ---------------------------------------------------------------- movements


@dataclass(slots=True)
class TickerMovements:
    """One ticker's stored movements, as the API serves them."""

    symbol: str
    http_status: int
    status: str | None
    job_id: int | None
    movements_total: int
    by_news_status: dict[str, int] = field(default_factory=dict)
    with_links: int = 0
    links: int = 0
    first_date: str | None = None
    latest_date: str | None = None
    latest_news_status: str | None = None
    latest_links: int = 0
    price_bars: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def links_per_movement(self) -> float:
        return round(self.links / self.movements_total, 2) if self.movements_total else 0.0


def summarize_movements(symbol: str, pages: list[tuple[int, dict[str, Any]]]) -> TickerMovements:
    """Fold the pages of one ticker's GET /tickers/{symbol} into a summary."""
    http_status, first = pages[0]
    movements = [m for _, page in pages for m in (page.get("movements") or [])]
    statuses = Counter(str(m.get("news_status")) for m in movements)
    by_date = sorted(movements, key=lambda m: m["date"])
    latest = by_date[-1] if by_date else None
    return TickerMovements(
        symbol=symbol,
        http_status=http_status,
        status=first.get("status"),
        job_id=first.get("job_id"),
        movements_total=int((first.get("pagination") or {}).get("total") or 0),
        by_news_status=dict(sorted(statuses.items())),
        with_links=sum(1 for m in movements if m.get("news")),
        links=sum(len(m.get("news") or []) for m in movements),
        first_date=by_date[0]["date"] if by_date else None,
        latest_date=latest["date"] if latest else None,
        latest_news_status=latest.get("news_status") if latest else None,
        latest_links=len(latest.get("news") or []) if latest else 0,
        price_bars=int((first.get("price_range") or {}).get("bars") or 0),
        warnings=list(first.get("warnings") or []),
    )


def fetch_movements(client: httpx.Client, symbol: str) -> TickerMovements:
    pages: list[tuple[int, dict[str, Any]]] = []
    offset = 0
    while True:
        response = client.get(
            f"/tickers/{symbol}", params={"limit": PAGE_SIZE, "offset": offset}
        )
        body = response.json() if response.content else {}
        pages.append((response.status_code, body))
        if response.status_code >= 400:
            break
        total = int((body.get("pagination") or {}).get("total") or 0)
        offset += PAGE_SIZE
        if offset >= total:
            break
    return summarize_movements(symbol, pages)


def cmd_movements(args: argparse.Namespace) -> int:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    with httpx.Client(base_url=args.base_url, timeout=HTTP_TIMEOUT_SECONDS) as client:
        tickers = [fetch_movements(client, symbol) for symbol in symbols]

    print("| Ticker | Status | Movements | complete | partial | pending | failed | "
          "With links | Links/move | Latest move (status, links) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for t in tickers:
        s = t.by_news_status
        print(
            f"| {t.symbol} | {t.status} | {t.movements_total} | {s.get('complete', 0)} | "
            f"{s.get('partial', 0)} | {s.get('pending', 0)} | {s.get('failed', 0)} | "
            f"{t.with_links} | {t.links_per_movement} | "
            f"{t.latest_date} ({t.latest_news_status}, {t.latest_links}) |"
        )
    queued = [t.symbol for t in tickers if t.job_id is not None]
    if queued:
        print(f"WARNING: work was queued for {queued}; this was meant to be read-only.")

    total = sum(t.movements_total for t in tickers)
    write_json(
        args.json_out,
        {
            "provenance": provenance("movements", args.base_url),
            "symbols": symbols,
            "movements_total": total,
            "movements_per_ticker_mean": round(total / len(tickers), 2) if tickers else 0,
            "tickers": [asdict(t) | {"links_per_movement": t.links_per_movement} for t in tickers],
        },
    )
    return 1 if queued else 0


# -------------------------------------------------------------------- usage


@dataclass(slots=True)
class OperationTotal:
    provider: str
    operation: str
    calls: int = 0
    cost_usd: Decimal = Decimal("0")
    estimated_calls: int = 0


def aggregate_usage(days: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum /admin/usage days, and derive spend per enrichment.

    An enrichment that found candidates makes exactly one relevance call
    (app/services/relevance.py), so relevance calls count enrichments. Its
    cost is that call plus its share of Exa searches (up to three per
    movement, fewer when the Hard tier's sector cache hits) and of peer
    lookups (one per ticker, cached). Exa searches also include the nightly
    sector pre-warm, and enrichments that found nothing make searches but no
    relevance call, so the per-enrichment figure slightly overstates a single
    scored movement's cost. The parts are reported separately for that reason.
    """
    totals: dict[tuple[str, str], OperationTotal] = {}
    background = interactive = Decimal("0")
    for day in days:
        background += Decimal(str(day.get("background_usd") or 0))
        interactive += Decimal(str(day.get("interactive_usd") or 0))
        for row in day.get("by_operation") or []:
            key = (row["provider"], row["operation"])
            total = totals.setdefault(key, OperationTotal(*key))
            total.calls += int(row.get("calls") or 0)
            total.cost_usd += Decimal(str(row.get("cost_usd") or 0))
            total.estimated_calls += int(row.get("estimated_calls") or 0)

    def by_operation(operation: str) -> OperationTotal:
        calls = sum(t.calls for (_, op), t in totals.items() if op == operation)
        cost = sum((t.cost_usd for (_, op), t in totals.items() if op == operation), Decimal("0"))
        return OperationTotal("*", operation, calls, cost)

    exa = totals.get(EXA_SEARCH, OperationTotal(*EXA_SEARCH))
    relevance = by_operation(OPERATION_RELEVANCE)
    peers = by_operation(OPERATION_PEERS)
    chat = by_operation(OPERATION_CHAT)
    enrichment_cost = exa.cost_usd + relevance.cost_usd + peers.cost_usd

    def per(cost: Decimal, calls: int) -> str | None:
        return str((cost / calls).quantize(Decimal("0.000001"))) if calls else None

    return {
        "days": len(days),
        "total_usd": str(background + interactive),
        "background_usd": str(background),
        "interactive_usd": str(interactive),
        "enrichments_scored": relevance.calls,
        "exa_searches": exa.calls,
        "exa_searches_per_enrichment": round(exa.calls / relevance.calls, 2) if relevance.calls else None,
        "spend_per_enrichment_usd": per(enrichment_cost, relevance.calls),
        "relevance_cost_per_enrichment_usd": per(relevance.cost_usd, relevance.calls),
        "exa_cost_per_search_usd": per(exa.cost_usd, exa.calls),
        "chat_turns": chat.calls,
        "spend_per_chat_turn_usd": per(chat.cost_usd, chat.calls),
        "estimated_calls": sum(t.estimated_calls for t in totals.values()),
        "by_operation": [
            asdict(t) | {"cost_usd": str(t.cost_usd)}
            for t in sorted(totals.values(), key=lambda t: t.cost_usd, reverse=True)
        ],
    }


def cmd_usage(args: argparse.Namespace) -> int:
    token = os.environ.get("METRIX_ADMIN_TOKEN", "").strip()
    if not token:
        print("Set METRIX_ADMIN_TOKEN (the API's ADMIN_TOKEN).", file=sys.stderr)
        return 2
    end = args.end or datetime.now(timezone.utc).date()
    days = [end - timedelta(days=offset) for offset in range(args.days)][::-1]
    pages: list[dict[str, Any]] = []
    with httpx.Client(
        base_url=args.base_url,
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"X-Admin-Token": token},
    ) as client:
        for day in days:
            response = client.get("/admin/usage", params={"date": day.isoformat()})
            if response.status_code != 200:
                print(f"{day}: HTTP {response.status_code}", file=sys.stderr)
                return 1
            pages.append(response.json())

    summary = aggregate_usage(pages)
    for key in (
        "days", "total_usd", "background_usd", "interactive_usd", "enrichments_scored",
        "exa_searches", "exa_searches_per_enrichment", "spend_per_enrichment_usd",
        "relevance_cost_per_enrichment_usd", "exa_cost_per_search_usd", "chat_turns",
        "spend_per_chat_turn_usd", "estimated_calls",
    ):
        print(f"{key}: {summary[key]}")
    write_json(
        args.json_out,
        {
            "provenance": provenance("usage", args.base_url)
            | {"first_day": days[0].isoformat(), "last_day": days[-1].isoformat()},
            "summary": summary,
            "per_day": pages,
        },
    )
    return 0


# --------------------------------------------------------------------- chat


@dataclass(slots=True)
class ChatTiming:
    question: str
    http_status: int
    latency_s: float
    grounded: bool | None = None
    answer_chars: int = 0
    cited: list[str] = field(default_factory=list)
    returned_movement_sources: int = 0
    returned_article_sources: int = 0
    returned_but_not_cited: int = 0
    cited_but_not_returned: list[str] = field(default_factory=list)
    markdown: list[str] = field(default_factory=list)
    error: str | None = None


def analyse_answer(question: str, status: int, latency_s: float, body: dict[str, Any]) -> ChatTiming:
    """How one answer cites: the labels in its text against the sources returned."""
    if status != 200:
        return ChatTiming(question, status, round(latency_s, 3), error=str(body.get("detail") or body)[:300])
    answer = str(body.get("answer") or "")
    sources = body.get("sources") or {}
    returned = {s["ref"] for s in sources.get("movements") or []} | {
        s["ref"] for s in sources.get("articles") or []
    }
    cited = sorted(set(CITATION.findall(answer)), key=lambda ref: (ref[0], int(ref[1:])))
    return ChatTiming(
        question=question,
        http_status=status,
        latency_s=round(latency_s, 3),
        grounded=body.get("grounded"),
        answer_chars=len(answer),
        cited=cited,
        returned_movement_sources=len(sources.get("movements") or []),
        returned_article_sources=len(sources.get("articles") or []),
        returned_but_not_cited=len(returned - set(cited)),
        cited_but_not_returned=sorted(set(cited) - returned),
        markdown=[name for name, pattern in MARKDOWN_MARKERS.items() if pattern.search(answer)],
    )


def ask(client: httpx.Client, question: str, ticker: str) -> ChatTiming:
    payload = {"ticker": ticker, "question": question}
    for attempt in range(2):
        started = time.perf_counter()
        response = client.post("/chat", json=payload)
        elapsed = time.perf_counter() - started
        retry_after = float(response.headers.get("retry-after") or 0)
        if response.status_code == 429 and attempt == 0 and 0 < retry_after <= MAX_RETRY_AFTER_SECONDS:
            print(f"rate limited; waiting {retry_after:.0f}s (not timed)")
            time.sleep(retry_after)
            continue
        break
    body = response.json() if response.content else {}
    return analyse_answer(question, response.status_code, elapsed, body)


def cmd_chat(args: argparse.Namespace) -> int:
    headers = {}
    key = os.environ.get("METRIX_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    with httpx.Client(base_url=args.base_url, timeout=HTTP_TIMEOUT_SECONDS, headers=headers) as client:
        timings = [ask(client, question, args.ticker) for question in CHAT_QUESTIONS]

    for t in timings:
        print(
            f"{t.latency_s:6.2f}s  HTTP {t.http_status}  cited {len(t.cited)}  "
            f"returned {t.returned_movement_sources}M+{t.returned_article_sources}A  "
            f"markdown {t.markdown or '-'}  {t.question}"
        )
    ok = [t.latency_s for t in timings if t.http_status == 200]
    write_json(
        args.json_out,
        {
            "provenance": provenance("chat", args.base_url, args.label)
            | {"ticker": args.ticker, "authenticated": bool(key)},
            "latency_s": {"min": min(ok), "max": max(ok), "mean": round(sum(ok) / len(ok), 3)} if ok else None,
            "answers": [asdict(t) for t in timings],
        },
    )
    return 0 if len(ok) == len(timings) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    movements = sub.add_parser("movements", help="movements and evidence per seed ticker")
    movements.add_argument("--base-url", required=True)
    movements.add_argument("--symbols", default=",".join(PRODUCTION_SEED_SYMBOLS))
    movements.add_argument("--json-out", type=Path)

    usage = sub.add_parser("usage", help="spend per enrichment from /admin/usage")
    usage.add_argument("--base-url", required=True)
    usage.add_argument("--days", type=int, default=DEFAULT_USAGE_DAYS)
    usage.add_argument("--end", type=date.fromisoformat, help="last UTC day (default today)")
    usage.add_argument("--json-out", type=Path)

    chat = sub.add_parser("chat", help="latency and citations for three fixed questions")
    chat.add_argument("--base-url", required=True)
    chat.add_argument("--label", required=True, help="where this ran, e.g. local or production")
    chat.add_argument("--ticker", default=CHAT_TICKER)
    chat.add_argument("--json-out", type=Path)

    args = parser.parse_args(argv)
    handlers = {"movements": cmd_movements, "usage": cmd_usage, "chat": cmd_chat}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
