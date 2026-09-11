#!/usr/bin/env python3
"""Readable terminal view of the Metrix API.

The API returns deeply nested JSON -- movement, then the articles that explain
it, then each article's tier, score and rationale. That structure is the whole
product, and raw JSON in a terminal hides it. This renders the same payload as
an indented tree so the hierarchy is scannable.

It is a client, not a shortcut: it talks to the HTTP API exactly as any other
consumer would, so if something is awkward to render here, the API shape is
wrong.

    scripts/show.py DHI
    scripts/show.py DHI --tier hard --direction up
    scripts/show.py DHI --ask "what drove the biggest drop?"

On colour
---------
Only the 16 basic ANSI colours are used, never 256-colour or truecolour. Those
hardcode specific RGB values, which fight the user's terminal theme -- a
"light grey" chosen for a dark terminal is invisible on a light one. The basic
codes are re-mapped by the terminal to whatever the user's scheme says, so the
output sits inside their theme instead of fighting it.

Colour is never the only signal. Direction carries a glyph and a sign, tiers
are spelled out, and scores are printed as numbers. Piped to a file, viewed
through `less`, or read by someone who cannot distinguish red from green, the
output loses emphasis but no information.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import textwrap
from typing import Any

import httpx

DEFAULT_BASE_URL = os.environ.get("METRIX_URL", "http://localhost:8000")


# --------------------------------------------------------------------- colour


class Palette:
    """Semantic roles mapped to ANSI codes.

    Callers ask for a *role* (`tier_hard`, `down`), never a colour. The mapping
    lives here alone, so changing the scheme is one edit and no call site knows
    what red means.
    """

    _CODES = {
        "heading": "1",  # bold
        "dim": "2",  # dim
        "up": "32",  # green
        "down": "31",  # red
        "tier_easy": "36",  # cyan
        "tier_medium": "33",  # yellow
        "tier_hard": "35",  # magenta
        "ref": "36",
        "strong": "1",
        "warn": "33",
        "error": "31",
    }

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, role: str, text: str) -> str:
        code = self._CODES.get(role)
        if not self.enabled or code is None:
            return text
        return f"\033[{code}m{text}\033[0m"

    def tier(self, tier_name: str, text: str) -> str:
        return self(f"tier_{tier_name}", text)


def should_colorize(choice: str) -> bool:
    """auto | always | never, honouring the NO_COLOR convention.

    `auto` disables colour when stdout is not a terminal, so piping to a file
    or into `grep` yields clean text rather than escape sequences.
    """
    if choice == "always":
        return True
    if choice == "never":
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


# ------------------------------------------------------------------ utilities


def terminal_width(maximum: int = 100) -> int:
    return min(shutil.get_terminal_size((80, 24)).columns, maximum)


SPARK_LEVELS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 48) -> str:
    """A compact price history. Flat series render as a single mid-level band."""
    if not values:
        return ""
    if len(values) > width:
        # Sample evenly rather than averaging: we want the shape, and averaging
        # would flatten exactly the spikes this tool exists to show.
        step = len(values) / width
        values = [values[min(int(i * step), len(values) - 1)] for i in range(width)]

    low, high = min(values), max(values)
    if high == low:
        return SPARK_LEVELS[len(SPARK_LEVELS) // 2] * len(values)
    span = high - low
    return "".join(
        SPARK_LEVELS[min(int((v - low) / span * len(SPARK_LEVELS)), len(SPARK_LEVELS) - 1)]
        for v in values
    )


def wrap(text: str, width: int, indent: str) -> list[str]:
    return textwrap.wrap(text, width=max(width - len(indent), 20)) or [""]


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ------------------------------------------------------------------ rendering


def render_ticker(payload: dict[str, Any], c: Palette) -> str:
    width = terminal_width()
    out: list[str] = []

    ticker = payload["ticker"]
    name = ticker.get("company_name") or ticker["symbol"]
    out.append(f"{c('heading', name)}  {c('dim', ticker['symbol'])}")

    facts = [
        ticker.get("sector"),
        ticker.get("industry"),
        ticker.get("exchange"),
        ticker.get("currency"),
    ]
    detail = " · ".join(f for f in facts if f)
    if detail:
        out.append(c("dim", detail))

    price_range = payload["price_range"]
    line = f"{price_range['bars']} bars"
    if price_range["start"]:
        line += f"  {price_range['start']} → {price_range['end']}"
    prices = payload.get("prices")
    if prices:
        closes = [bar["adj_close"] for bar in prices]
        line += f"  {sparkline(closes)}  {closes[0]:,.2f} → {closes[-1]:,.2f}"
    out.append(c("dim", line))

    status = payload["status"]
    status_role = {"ready": "up", "failed": "error"}.get(status, "warn")
    pagination = payload["pagination"]
    shown = c("dim", f", showing {pagination['returned']}")
    out.append(
        f"{c(status_role, status)}{c('dim', ' · ')}"
        f"{pagination['total']} movement(s){shown}"
    )

    if payload.get("message"):
        out.append(c("warn", f"! {payload['message']}"))

    filters = {k: v for k, v in payload["filters"].items() if v not in (None, [])}
    if filters:
        out.append(c("dim", f"filters: {json.dumps(filters, default=str)}"))

    out.append("")

    if not payload["movements"]:
        out.append(c("dim", "  no movements match these filters"))
    for movement in payload["movements"]:
        out.extend(render_movement(movement, c, width))

    for warning in payload.get("warnings", []):
        out.append(c("warn", f"! {warning}"))

    return "\n".join(out)


def render_movement(movement: dict[str, Any], c: Palette, width: int) -> list[str]:
    up = movement["direction"] == "up"
    glyph = "▲" if up else "▼"  # so direction survives without colour
    role = "up" if up else "down"

    sigma = movement.get("sigma_multiple")
    sigma_text = f"{sigma:.1f}σ" if sigma else "—"

    move = c(role, f"{glyph} {movement['daily_return_pct']:+.2f}%")
    threshold = movement["threshold"] * 100
    audit = c(
        "dim",
        f"{sigma_text}  bar {threshold:.2f}% ({movement['threshold_source']})",
    )
    lines = [f"  {c('heading', movement['date'])}  {move}  {audit}"]

    news = movement.get("news", [])
    if not news:
        note = {
            "failed": "news lookup failed for this movement",
            "pending": "news not fetched yet",
        }.get(movement.get("news_status"), "no explanatory news found")
        lines.append(f"    {c('dim', note)}")
        lines.append("")
        return lines

    for index, link in enumerate(news):
        last = index == len(news) - 1
        lines.extend(render_article(link, c, width, last=last))
    lines.append("")
    return lines


def render_article(
    link: dict[str, Any], c: Palette, width: int, *, last: bool
) -> list[str]:
    article = link["article"]
    branch = "└─" if last else "├─"
    trunk = "  " if last else "│ "

    tier = link["relevance_tier"]
    score = link["relevance_score"]
    # Confidence is carried by weight, not by a second hue -- the hue is
    # already spoken for by the tier, and two colour axes is a rainbow.
    score_role = "strong" if score >= 0.7 else ("dim" if score < 0.5 else "")
    score_text = c(score_role, f"{score:.2f}") if score_role else f"{score:.2f}"

    published = (article.get("published_at") or "")[:10] or "date unknown"
    meta = f"{article.get('source') or 'unknown'} · {published}"

    lines = [
        f"    {c('dim', branch)} {c.tier(tier, f'{tier:<6}')} {score_text}  {c('dim', meta)}"
    ]
    title_indent = f"    {c('dim', trunk)}  "
    for chunk in wrap(truncate(article.get("title") or "(untitled)", 300), width, "        "):
        lines.append(f"{title_indent}{chunk}")

    rationale = link.get("rationale")
    if rationale:
        for i, chunk in enumerate(wrap(rationale, width, "          ")):
            prefix = "→ " if i == 0 else "  "
            lines.append(f"{title_indent}{c('dim', prefix + chunk)}")
    return lines


# Observed citation forms: [M3], [A1][A2], [A18, A19], and [M12, +7.80% on
# 2026-01-09]. Rather than enumerate them, match any short bracket and pull the
# ref tokens out of whatever is inside -- a renderer should not be brittle to
# the model's formatting. Brackets containing no ref (ordinary markdown links)
# are left alone.
REF_GROUP = re.compile(r"\[[^\[\]]{0,160}\]")
REF_TOKEN = re.compile(r"\b([MA]\d+)\b")


def cited_refs(answer: str) -> set[str]:
    return {
        token
        for group in REF_GROUP.findall(answer)
        for token in REF_TOKEN.findall(group)
    }


def highlight_refs(text: str, c: Palette) -> str:
    def paint(match: re.Match[str]) -> str:
        group = match.group(0)
        return c("ref", group) if REF_TOKEN.search(group) else group

    return REF_GROUP.sub(paint, text)


def render_chat(payload: dict[str, Any], c: Palette) -> str:
    width = terminal_width()
    out: list[str] = []

    header = f"{c('heading', payload.get('ticker') or 'no ticker')}"
    if not payload.get("grounded"):
        header += c("warn", "  (ungrounded — no stored data matched)")
    out.append(header)
    out.append(c("dim", f"conversation {payload['conversation_id']}"))
    out.append("")

    answer = payload["answer"]
    for paragraph in answer.split("\n"):
        if not paragraph.strip():
            out.append("")
            continue
        for chunk in textwrap.wrap(paragraph, width=width) or [""]:
            out.append(highlight_refs(chunk, c))

    out.extend(render_sources(answer, payload.get("sources", {}), c, width))
    return "\n".join(out)


def render_sources(
    answer: str, sources: dict[str, Any], c: Palette, width: int
) -> list[str]:
    """List the sources the answer actually cited, not everything retrieved.

    Retrieval deliberately over-fetches -- every movement on record goes into
    the prompt -- so printing the whole set buries the two or three references
    that carry the answer. The uncited remainder is reported as a count, which
    is the part a reader cares about: how much was available but unused.
    """
    cited = cited_refs(answer)
    movements = sources.get("movements", [])
    articles = sources.get("articles", [])
    if not movements and not articles:
        return []

    lines = ["", c("dim", "cited")]
    used = False

    for movement in movements:
        if movement["ref"] not in cited:
            continue
        used = True
        up = movement["direction"] == "up"
        glyph = "▲" if up else "▼"
        move = c("up" if up else "down", f"{glyph} {movement['daily_return_pct']:+.2f}%")
        lines.append(f"  {c('ref', movement['ref']):<4} {movement['date']}  {move}")

    for article in articles:
        if article["ref"] not in cited:
            continue
        used = True
        tier = article["relevance_tier"]
        lines.append(
            f"  {c('ref', article['ref']):<4} {c.tier(tier, f'{tier:<6}')} "
            f"{truncate(article.get('title') or article['url'], width - 26)}"
        )
        lines.append(f"       {c('dim', article['url'])}")

    if not used:
        lines.append(c("dim", "  (the answer cited nothing)"))

    uncited = len([a for a in articles if a["ref"] not in cited])
    if uncited:
        lines.append(
            c("dim", f"  + {uncited} more article(s) retrieved but not cited")
        )
    return lines


# ------------------------------------------------------------------------ cli


def fetch_ticker(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = {
        "limit": args.limit,
        "include_prices": "true" if not args.no_sparkline else "false",
    }
    if args.wait:
        params["wait"] = "true"
    if args.refresh:
        params["refresh"] = "true"
    if args.start:
        params["start"] = args.start
    if args.end:
        params["end"] = args.end
    if args.direction:
        params["direction"] = args.direction
    if args.min_pct is not None:
        params["min_magnitude_pct"] = args.min_pct
    if args.tier:
        params["tier"] = args.tier

    response = httpx.get(
        f"{args.base_url}/tickers/{args.ticker}", params=params, timeout=args.timeout
    )
    return unwrap(response)


def ask(args: argparse.Namespace) -> dict[str, Any]:
    body: dict[str, Any] = {"ticker": args.ticker, "question": args.ask}
    if args.conversation:
        body["conversation_id"] = args.conversation
    response = httpx.post(f"{args.base_url}/chat", json=body, timeout=args.timeout)
    return unwrap(response)


def unwrap(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        raise SystemExit(f"non-JSON response (HTTP {response.status_code})") from None

    if "error" in payload and "detail" in payload:
        raise SystemExit(f"{payload['error']}: {payload['detail']}")
    if response.status_code >= 400 and "status" not in payload:
        raise SystemExit(f"HTTP {response.status_code}: {json.dumps(payload)[:300]}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="show.py",
        description="Readable terminal view of the Metrix API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              scripts/show.py DHI
              scripts/show.py DHI --tier hard
              scripts/show.py NVDA --wait               # ingest inline if cold
              scripts/show.py DHI --direction down --min-pct 4
              scripts/show.py DHI --ask "what drove the biggest drop?"
            """
        ),
    )
    parser.add_argument("ticker", help="Ticker symbol, e.g. DHI")
    parser.add_argument("--ask", metavar="QUESTION", help="Ask the chat endpoint instead.")
    parser.add_argument("--conversation", help="Continue an existing conversation id.")

    parser.add_argument("--start", help="Only movements on or after this date.")
    parser.add_argument("--end", help="Only movements on or before this date.")
    parser.add_argument(
        "--tier",
        action="append",
        choices=["easy", "medium", "hard"],
        help="Relevance tier. Repeat for several.",
    )
    parser.add_argument("--direction", choices=["up", "down"])
    parser.add_argument("--min-pct", type=float, help="Minimum move size, in percent.")
    parser.add_argument("--limit", type=int, default=10)

    parser.add_argument(
        "--wait", action="store_true", help="Ingest inline if the ticker is cold (slow)."
    )
    parser.add_argument("--refresh", action="store_true", help="Force re-ingestion.")
    parser.add_argument("--no-sparkline", action="store_true", help="Skip fetching prices.")

    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    palette = Palette(should_colorize(args.color))

    try:
        payload = ask(args) if args.ask else fetch_ticker(args)
    except httpx.RequestError as exc:
        raise SystemExit(
            f"could not reach the API at {args.base_url} ({exc}). "
            "Is it running? `docker compose up -d`"
        ) from None

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(render_chat(payload, palette) if args.ask else render_ticker(payload, palette))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
