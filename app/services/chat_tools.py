"""The tools Ask Metrix may call. Read-only, database only.

No tool fetches prices, searches news or calls a model: a question can only
be answered from what ingestion stored, which is what makes every answer
checkable. Each row a tool returns carries a stable citation label built
from its database id (`M412` a movement, `A1033` an article, `F7` a
filing). The labels are registered on the turn (`Turn.sources`) as rows
are returned, and after the answer is written only labels registered this
turn survive (`app.services.chat`).

Results are compact JSON, capped in rows and characters, so a tool call
costs a bounded number of tokens.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.logging import get_logger
from app.core.symbols import is_valid_symbol, normalize_symbol
from app.models.filings import Filing, MovementFiling
from app.models.market import Movement, PriceBar, Ticker
from app.models.news import MovementNewsLink, NewsArticle
from app.schemas.chat import ArticleSource, FilingSource, MovementSource
from app.services import evidence, returns, symbol_directory
from app.services.auth import Principal
from app.services.edgar import ITEM_EVENTS
from app.services.llm.base import ToolCall, ToolSpec

logger = get_logger(__name__)

MAX_RESULT_CHARS = 8000
LABEL = re.compile(r"\b([MAF])(\d+)\b")


@dataclass(slots=True)
class Turn:
    """What one chat turn's tools have returned so far."""

    session: AsyncSession
    principal: Principal
    movements: dict[str, MovementSource] = field(default_factory=dict)
    articles: dict[str, ArticleSource] = field(default_factory=dict)
    filings: dict[str, FilingSource] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)

    @property
    def labels(self) -> set[str]:
        return set(self.movements) | set(self.articles) | set(self.filings)

    def movement(self, m: Movement, symbol: str) -> str:
        label = f"M{m.id}"
        self.movements.setdefault(
            label,
            MovementSource(ref=label, movement_id=m.id, symbol=symbol, date=m.date,
                           daily_return_pct=round(m.daily_return * 100, 4), direction=m.direction.value),
        )
        self._symbol(symbol)
        return label

    def article(self, article: NewsArticle, link: MovementNewsLink | None = None) -> str:
        label = f"A{article.id}"
        self.articles.setdefault(
            label,
            ArticleSource(
                ref=label, article_id=article.id, title=article.title, url=article.url,
                source=article.source_domain,
                published_at=article.published_at.date() if article.published_at else None,
                relevance_tier=link.relevance_tier.value if link is not None else None,
                relevance_score=link.relevance_score if link is not None else None,
            ),
        )
        return label

    def filing(self, filing: Filing, symbol: str) -> str:
        label = f"F{filing.id}"
        self.filings.setdefault(
            label,
            FilingSource(ref=label, filing_id=filing.id, symbol=symbol, accession=filing.accession,
                         form=filing.form, items=list(filing.items or []), url=filing.url,
                         accepted_at=filing.accepted_at),
        )
        return label

    def _symbol(self, symbol: str) -> None:
        if symbol not in self.symbols:
            self.symbols.append(symbol)


# ------------------------------------------------------------- definitions

_DATE = {"type": "string", "description": "YYYY-MM-DD"}
_SYMBOL = {"type": "string", "description": "Ticker symbol, e.g. NVDA"}

TOOLS: list[ToolSpec] = [
    ToolSpec(
        "get_movements",
        "List a ticker's flagged movements (unusually large daily moves) from the database, with each "
        "move's decomposition into market, sector and company parts, its evidence status, and its "
        "strongest linked article. Use first to find which days to look at.",
        {
            "type": "object",
            "properties": {
                "symbol": _SYMBOL,
                "start": _DATE,
                "end": _DATE,
                "direction": {"type": "string", "enum": ["up", "down"]},
                "min_abs_return_pct": {"type": "number", "description": "e.g. 5 for moves of 5% or more"},
                "dominant": {"type": "string", "enum": ["market", "sector", "company"]},
                "sort": {"type": "string", "enum": ["date", "abs_return", "abs_sar"], "description": "largest first; default date (newest)"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "get_movement_case",
        "Everything stored about one movement: the decomposition, the opening gap and intraday legs, "
        "relative volume, the drift after it, all current linked articles with timing, role and the "
        "scorer's rationale, the SEC filings around it, and how its benchmarks and peers did that session.",
        {
            "type": "object",
            "properties": {"symbol": _SYMBOL, "date": _DATE},
            "required": ["symbol", "date"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "search_evidence",
        "Full-text search over the articles linked to stored movements (title, summary, body). Use for "
        "themes and causes across days or tickers, e.g. 'export restrictions' or 'guidance cut'.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "symbol": _SYMBOL,
                "start": _DATE,
                "end": _DATE,
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "get_filings",
        "A ticker's stored SEC filings (8-K with item numbers, 10-Q, 10-K, offerings, 13D).",
        {
            "type": "object",
            "properties": {
                "symbol": _SYMBOL,
                "form": {"type": "string", "description": "e.g. 8-K"},
                "items": {"type": "array", "items": {"type": "string"}, "description": "8-K items, e.g. ['2.02']"},
                "start": _DATE,
                "end": _DATE,
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "compare",
        "Total return of up to five tickers over a date range against SPY, from stored daily prices, "
        "and how many flagged movements each had in it. Default range: the last 3 months.",
        {
            "type": "object",
            "properties": {
                "symbols": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
                "start": _DATE,
                "end": _DATE,
            },
            "required": ["symbols"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "get_watchlist_digest",
        "One of the user's own watchlists: each holding's return and contribution over the last sessions, "
        "and its flagged moves.",
        {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "sessions": {"type": "integer", "minimum": 1, "maximum": 60}},
            "required": ["id"],
            "additionalProperties": False,
        },
    ),
]
TOOL_NAMES = {t.name for t in TOOLS}


class ToolInputError(ValueError):
    pass


# --------------------------------------------------------------- dispatch


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str  # what the model sees
    summary: str  # one line for the stream and the logs
    rows: int


async def run(turn: Turn, call: ToolCall) -> ToolResult:
    """Run one tool call. Bad input or an unknown tool is an error result the
    model can read and correct, never an exception."""
    if call.error:
        return _error(call.error)
    if call.name not in TOOL_NAMES:
        return _error(f"unknown tool {call.name!r}")
    handler = _HANDLERS[call.name]
    try:
        payload, summary, rows = await handler(turn, call.arguments)
    except ToolInputError as exc:
        return _error(str(exc))
    content = json.dumps(payload, separators=(",", ":"), default=str)
    if len(content) > MAX_RESULT_CHARS:
        content = content[: MAX_RESULT_CHARS - 40] + '..."(truncated)"'
    logger.info("chat_tool", tool=call.name, rows=rows, chars=len(content))
    return ToolResult(content, summary, rows)


def _error(message: str) -> ToolResult:
    return ToolResult(json.dumps({"error": message}), f"error: {message}", 0)


# ---------------------------------------------------------------- helpers


def _date(value: Any, name: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ToolInputError(f"{name} must be YYYY-MM-DD, not {value!r}") from None


async def _ticker(session: AsyncSession, raw: Any, *, prices_only: bool = False) -> Ticker:
    """A ticker Metrix has researched; with `prices_only`, any with stored
    prices (benchmarks and peers are priced but never researched)."""
    symbol = normalize_symbol(str(raw or ""))
    if not is_valid_symbol(symbol):
        raise ToolInputError(f"{raw!r} is not a ticker symbol")
    symbol = symbol_directory.canonical(symbol)
    ticker = await session.scalar(sa.select(Ticker).where(Ticker.symbol == symbol))
    if ticker is not None and ticker.last_ingested_at is not None:
        return ticker
    if ticker is not None and prices_only and await session.scalar(
        sa.select(PriceBar.id).where(PriceBar.ticker_id == ticker.id).limit(1)
    ):
        return ticker
    raise ToolInputError(f"Metrix has no data for {symbol}; it has not been researched yet")


def _pct(value: float | None) -> float | None:
    return None if value is None else round(value * 100, 2)


def _current_links(movement: Movement) -> list[MovementNewsLink]:
    return sorted(
        (link for link in movement.news_links if link.superseded_at is None),
        key=lambda link: link.relevance_score,
        reverse=True,
    )


def _movement_row(turn: Turn, m: Movement, symbol: str) -> dict[str, Any]:
    links = _current_links(m)
    top = links[0] if links else None
    filings_judged = [mf for mf in m.filings if mf.superseded_at is None and mf.relevance_score is not None]
    status = evidence.status_for(m, has_evidence=bool(links) or bool(filings_judged), job=None,
                                 now=datetime.now(timezone.utc))
    row: dict[str, Any] = {
        "label": turn.movement(m, symbol),
        "symbol": symbol,
        "date": m.date.isoformat(),
        "return_pct": _pct(m.daily_return),
        "abnormal_return_pct": _pct(m.abnormal_return),
        "sar": None if m.sar is None else round(m.sar, 2),
        "dominant": m.dominant_component,
        "trigger": m.trigger or "raw",
        "evidence": status.state,
    }
    if top is not None:
        row["top_evidence"] = {
            "label": turn.article(top.article, top),
            "title": top.article.title,
            "tier": top.relevance_tier.value,
            "score": round(top.relevance_score, 2),
            "rationale": top.rationale,
        }
    return row


_MOVEMENT_OPTIONS = (
    selectinload(Movement.news_links).selectinload(MovementNewsLink.article),
    selectinload(Movement.filings),
)


# ------------------------------------------------------------------ tools


async def _get_movements(turn: Turn, args: dict[str, Any]) -> tuple[Any, str, int]:
    ticker = await _ticker(turn.session, args.get("symbol"))
    conditions = [Movement.ticker_id == ticker.id, Movement.qualifies.is_(True)]
    if start := _date(args.get("start"), "start"):
        conditions.append(Movement.date >= start)
    if end := _date(args.get("end"), "end"):
        conditions.append(Movement.date <= end)
    if args.get("direction") in ("up", "down"):
        conditions.append(Movement.direction == args["direction"])
    if args.get("min_abs_return_pct") is not None:
        conditions.append(Movement.abs_return >= float(args["min_abs_return_pct"]) / 100)
    if args.get("dominant") in ("market", "sector", "company"):
        conditions.append(Movement.dominant_component == args["dominant"])
    order = {
        "abs_return": (Movement.abs_return.desc(),),
        "abs_sar": (sa.func.abs(sa.func.coalesce(Movement.sar, 0.0)).desc(),),
    }.get(args.get("sort") or "date", ())
    limit = max(1, min(int(args.get("limit") or 10), 20))
    rows = (
        await turn.session.scalars(
            sa.select(Movement).where(*conditions).order_by(*order, Movement.date.desc()).limit(limit)
            .options(*_MOVEMENT_OPTIONS).execution_options(populate_existing=True)
        )
    ).all()
    total = await turn.session.scalar(sa.select(sa.func.count()).select_from(Movement).where(*conditions))
    payload = {
        "symbol": ticker.symbol,
        "company": ticker.company_name,
        "matching_movements": total,
        "movements": [_movement_row(turn, m, ticker.symbol) for m in rows],
    }
    return payload, f"{len(rows)} of {total} {ticker.symbol} movements", len(rows)


async def _get_movement_case(turn: Turn, args: dict[str, Any]) -> tuple[Any, str, int]:
    ticker = await _ticker(turn.session, args.get("symbol"))
    day = _date(args.get("date"), "date")
    if day is None:
        raise ToolInputError("date is required")
    m = await turn.session.scalar(
        sa.select(Movement).where(Movement.ticker_id == ticker.id, Movement.date == day)
        .options(*_MOVEMENT_OPTIONS).execution_options(populate_existing=True)
    )
    if m is None:
        nearby = (
            await turn.session.scalars(
                sa.select(Movement.date).where(
                    Movement.ticker_id == ticker.id, Movement.qualifies.is_(True),
                    Movement.date.between(day - timedelta(days=10), day + timedelta(days=10)),
                ).order_by(Movement.date)
            )
        ).all()
        raise ToolInputError(
            f"{ticker.symbol} has no flagged movement on {day.isoformat()}"
            + (f"; nearby: {', '.join(d.isoformat() for d in nearby)}" if nearby else "")
        )
    row = _movement_row(turn, m, ticker.symbol)
    row.pop("top_evidence", None)
    row["decomposition"] = None if m.decomposition_model is None else {
        "market_pct": _pct(m.market_component), "sector_pct": _pct(m.sector_component),
        "company_pct": _pct(m.abnormal_return), "market_benchmark": m.market_benchmark,
        "sector_benchmark": m.sector_benchmark, "r_squared": None if m.r_squared is None else round(m.r_squared, 2),
    }
    row["legs"] = {"gap_pct": _pct(m.gap_return), "intraday_pct": _pct(m.intraday_return)}
    row["relative_volume"] = None if m.relative_volume is None else round(m.relative_volume, 2)
    row["drift"] = {"car_5_pct": _pct(m.car_5), "car_20_pct": _pct(m.car_20),
                    "label_5": m.drift_5_label, "label_20": m.drift_20_label}
    row["articles"] = [
        {
            "label": turn.article(link.article, link),
            "title": link.article.title,
            "source": link.article.source_domain,
            "published": link.article.published_at.isoformat() if link.article.published_at else None,
            "timing": link.timing, "role": link.role, "tier": link.relevance_tier.value,
            "score": round(link.relevance_score, 2), "rationale": link.rationale,
        }
        for link in _current_links(m)[:6]
    ]
    row["filings"] = [
        {
            "label": turn.filing(mf.filing, ticker.symbol), "form": mf.filing.form,
            "events": [ITEM_EVENTS[i] for i in (mf.filing.items or []) if i in ITEM_EVENTS],
            "accepted_at": mf.filing.accepted_at.isoformat() if mf.filing.accepted_at else None,
            "timing": mf.timing,
            "verdict": None if mf.superseded_at is not None or mf.relevance_score is None
            else {"role": mf.role, "score": round(mf.relevance_score, 2), "rationale": mf.rationale},
        }
        for mf in m.filings
    ]
    peers = [p for p in (ticker.peers or {}).get("peers", []) if isinstance(p, dict)]
    peer_symbols = [normalize_symbol(p["ticker"]) for p in peers if p.get("ticker")]
    bench = [s for s in (m.market_benchmark, m.sector_benchmark) if s]
    same_day = await returns.session_returns(turn.session, bench + peer_symbols, day)
    row["benchmarks"] = {s: _pct(same_day.get(s)) for s in bench}
    row["peers"] = [
        {"name": p.get("name"), "symbol": p.get("ticker"), "relationship": p.get("relationship"),
         "return_pct": _pct(same_day.get(normalize_symbol(p["ticker"]))) if p.get("ticker") else None}
        for p in peers
    ]
    row["abstain_reason"] = m.abstain_reason
    return row, f"{ticker.symbol} {day.isoformat()}: {len(row['articles'])} articles, {len(row['filings'])} filings", 1


_search_column: dict[str, bool] = {}


async def _has_search_column(session: AsyncSession) -> bool:
    bind = session.get_bind()
    key = str(bind.url)
    if key not in _search_column:
        _search_column[key] = bool(
            await session.scalar(
                sa.text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'news_articles' AND column_name = 'search_vector'"
                )
            )
        )
    return _search_column[key]


async def _search_evidence(turn: Turn, args: dict[str, Any]) -> tuple[Any, str, int]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolInputError("query is required")
    limit = max(1, min(int(args.get("limit") or 8), 10))
    conditions = [MovementNewsLink.superseded_at.is_(None), Movement.qualifies.is_(True)]
    if args.get("symbol"):
        ticker = await _ticker(turn.session, args["symbol"])
        conditions.append(Movement.ticker_id == ticker.id)
    if start := _date(args.get("start"), "start"):
        conditions.append(Movement.date >= start)
    if end := _date(args.get("end"), "end"):
        conditions.append(Movement.date <= end)

    statement = (
        sa.select(MovementNewsLink, Movement, Ticker.symbol)
        .join(Movement, Movement.id == MovementNewsLink.movement_id)
        .join(Ticker, Ticker.id == Movement.ticker_id)
        .join(NewsArticle, NewsArticle.id == MovementNewsLink.article_id)
        .options(selectinload(MovementNewsLink.article))
        .where(*conditions)
    )
    if turn.session.get_bind().dialect.name == "postgresql":
        tsquery = sa.func.websearch_to_tsquery(sa.literal_column("'english'::regconfig"), query)
        if await _has_search_column(turn.session):
            vector = sa.literal_column("news_articles.search_vector")
        else:  # a schema built without migrations (tests): the same expression, unindexed
            vector = sa.func.to_tsvector(
                sa.literal_column("'english'::regconfig"),
                sa.func.coalesce(NewsArticle.title, "") + " " + sa.func.coalesce(NewsArticle.summary, "")
                + " " + sa.func.left(sa.func.coalesce(NewsArticle.content, ""), 20000),
            )
        statement = statement.where(vector.op("@@")(tsquery)).order_by(
            sa.func.ts_rank(vector, tsquery).desc(), MovementNewsLink.relevance_score.desc()
        )
    else:
        words = [w for w in re.findall(r"[\w'-]+", query.lower()) if len(w) > 2][:6] or [query.lower()]
        haystack = sa.func.lower(
            sa.func.coalesce(NewsArticle.title, "") + " " + sa.func.coalesce(NewsArticle.summary, "")
            + " " + sa.func.coalesce(NewsArticle.content, "")
        )
        statement = statement.where(*[haystack.like(f"%{w}%") for w in words]).order_by(
            MovementNewsLink.relevance_score.desc()
        )
    rows = (await turn.session.execute(statement.limit(limit))).all()
    hits = []
    for link, m, symbol in rows:
        hits.append(
            {
                "label": turn.article(link.article, link),
                "title": link.article.title,
                "source": link.article.source_domain,
                "published": link.article.published_at.date().isoformat() if link.article.published_at else None,
                "movement": {"label": turn.movement(m, symbol), "symbol": symbol, "date": m.date.isoformat(),
                             "return_pct": _pct(m.daily_return)},
                "tier": link.relevance_tier.value,
                "score": round(link.relevance_score, 2),
                "rationale": link.rationale,
            }
        )
    return {"query": query, "results": hits}, f"{len(hits)} articles for {query!r}", len(hits)


async def _get_filings(turn: Turn, args: dict[str, Any]) -> tuple[Any, str, int]:
    ticker = await _ticker(turn.session, args.get("symbol"))
    conditions = [Filing.ticker_id == ticker.id]
    if args.get("form"):
        conditions.append(Filing.form == str(args["form"]).upper())
    if start := _date(args.get("start"), "start"):
        conditions.append(Filing.filing_date >= start)
    if end := _date(args.get("end"), "end"):
        conditions.append(Filing.filing_date <= end)
    rows = (await turn.session.scalars(sa.select(Filing).where(*conditions).order_by(Filing.filing_date.desc()))).all()
    wanted = {str(i) for i in (args.get("items") or [])}
    if wanted:
        rows = [f for f in rows if wanted & set(f.items or [])]
    rows = rows[:20]
    attached: dict[int, list[str]] = {}
    if rows:
        for filing_id, movement_id in (
            await turn.session.execute(
                sa.select(MovementFiling.filing_id, MovementFiling.movement_id).where(
                    MovementFiling.filing_id.in_([f.id for f in rows])
                )
            )
        ).all():
            attached.setdefault(filing_id, []).append(f"M{movement_id}")
    payload = {
        "symbol": ticker.symbol,
        "filings": [
            {
                "label": turn.filing(f, ticker.symbol), "form": f.form, "items": list(f.items or []),
                "events": [ITEM_EVENTS[i] for i in (f.items or []) if i in ITEM_EVENTS],
                "filing_date": f.filing_date.isoformat(),
                "accepted_at": f.accepted_at.isoformat() if f.accepted_at else None,
                "near_movements": attached.get(f.id, []),
            }
            for f in rows
        ],
        "note": None if rows else "No filings stored (SEC EDGAR may be disabled).",
    }
    return payload, f"{len(rows)} {ticker.symbol} filings", len(rows)


async def _compare(turn: Turn, args: dict[str, Any]) -> tuple[Any, str, int]:
    raw = args.get("symbols") or []
    if not isinstance(raw, list) or not raw:
        raise ToolInputError("symbols must be a non-empty list")
    tickers = [await _ticker(turn.session, s, prices_only=True) for s in raw[:5]]
    market = settings.benchmark_market_symbol.strip().upper()
    symbols = [t.symbol for t in tickers]
    end = _date(args.get("end"), "end") or await turn.session.scalar(
        sa.select(sa.func.max(PriceBar.date)).join(Ticker, Ticker.id == PriceBar.ticker_id)
        .where(Ticker.symbol.in_(symbols))
    )
    if end is None:
        raise ToolInputError("no stored prices for those tickers")
    start = _date(args.get("start"), "start") or end - timedelta(days=91)
    if start >= end:
        raise ToolInputError("start must be before end")
    rows = (
        await turn.session.execute(
            sa.select(Ticker.symbol, PriceBar.date, PriceBar.adj_close)
            .join(PriceBar, PriceBar.ticker_id == Ticker.id)
            .where(Ticker.symbol.in_([*symbols, market]), PriceBar.date >= start, PriceBar.date <= end)
            .order_by(Ticker.symbol, PriceBar.date)
        )
    ).all()
    series: dict[str, list[tuple[date, float]]] = {}
    for symbol, day, close in rows:
        series.setdefault(symbol, []).append((day, float(close)))

    def total(symbol: str) -> tuple[float | None, str | None, str | None]:
        points = series.get(symbol, [])
        if len(points) < 2:
            return None, None, None
        return points[-1][1] / points[0][1] - 1, points[0][0].isoformat(), points[-1][0].isoformat()

    spy, _, _ = total(market)
    counts = dict(
        (
            await turn.session.execute(
                sa.select(Movement.ticker_id, sa.func.count())
                .where(Movement.ticker_id.in_([t.id for t in tickers]), Movement.qualifies.is_(True),
                       Movement.date >= start, Movement.date <= end)
                .group_by(Movement.ticker_id)
            )
        ).all()
    )
    out = []
    for ticker in tickers:
        value, first, last = total(ticker.symbol)
        out.append({
            "symbol": ticker.symbol, "from": first, "to": last, "return_pct": _pct(value),
            "vs_market_pct": _pct(value - spy) if value is not None and spy is not None else None,
            "flagged_movements": counts.get(ticker.id, 0),
        })
    payload = {"start": start.isoformat(), "end": end.isoformat(),
               "market": {"symbol": market, "return_pct": _pct(spy)}, "tickers": out}
    return payload, f"{', '.join(symbols)} against {market}", len(out)


async def _get_watchlist_digest(turn: Turn, args: dict[str, Any]) -> tuple[Any, str, int]:
    from fastapi import HTTPException

    from app.api.routes.watchlists import build_digest, get_owned

    try:
        watchlist = await get_owned(turn.session, turn.principal, int(args.get("id")))
    except (HTTPException, TypeError, ValueError):
        raise ToolInputError("no such watchlist (only your own lists are visible)") from None
    digest = await build_digest(turn.session, watchlist, max(1, min(int(args.get("sessions") or 5), 60)))
    movements = {
        (t.symbol, m.date): m
        for t, m in (
            await turn.session.execute(
                sa.select(Ticker, Movement).join(Movement, Movement.ticker_id == Ticker.id).where(
                    Ticker.symbol.in_([h.symbol for h in digest.holdings]), Movement.qualifies.is_(True),
                    Movement.date.in_([mv.date for h in digest.holdings for mv in h.moves] or [date.min]),
                )
            )
        ).all()
    }
    holdings = []
    for h in digest.holdings:
        holdings.append({
            "symbol": h.symbol, "weight": round(h.weight, 4), "period_return_pct": _pct(h.period_return),
            "contribution_pct": _pct(h.contribution),
            "moves": [
                {"label": turn.movement(movements[(h.symbol, mv.date)], h.symbol), "date": mv.date.isoformat(),
                 "return_pct": _pct(mv.daily_return), "evidence": mv.evidence, "explanation": mv.explanation}
                for mv in h.moves if (h.symbol, mv.date) in movements
            ],
        })
    payload = {"name": digest.name, "sessions": [d.isoformat() for d in digest.sessions],
               "period_return_pct": _pct(digest.period_return), "holdings": holdings, "missing": digest.missing}
    return payload, f"watchlist {digest.name!r}: {len(holdings)} holdings", len(holdings)


_HANDLERS = {
    "get_movements": _get_movements,
    "get_movement_case": _get_movement_case,
    "search_evidence": _search_evidence,
    "get_filings": _get_filings,
    "compare": _compare,
    "get_watchlist_digest": _get_watchlist_digest,
}


__all__ = ["LABEL", "TOOLS", "ToolResult", "Turn", "run"]
