"""Read-only research endpoints over stored data.

None of these fetch, search or call a model: they read what ingestion and
the nightly run stored, so they cost nothing and never enqueue work. A
ticker Metrix has not ingested is a 404 here; `GET /tickers/{symbol}` is the
endpoint that fetches.

    GET /symbols/search?q=                       symbol and company-name search
    GET /tickers/{symbol}/movements/{date}       one movement's case file
    GET /tickers/{symbol}/filings                its SEC filings
    GET /tickers/{symbol}/earnings               earnings releases (8-K Item 2.02)
    GET /market/pulse                            the largest abnormal moves, and sectors
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, SessionDep
from app.api.routes.tickers import _movement_out
from app.core.config import settings
from app.core.symbols import is_valid_symbol, normalize_symbol
from app.models.filings import Filing, MovementFiling
from app.models.market import Movement, PriceBar, Ticker
from app.models.news import MovementNewsLink
from app.models.symbols import ListedSymbol
from app.schemas.market import TickerOut
from app.schemas.research import (
    AbnormalTestOut,
    DetectionAuditOut,
    EarningsEventOut,
    EarningsMovementOut,
    EarningsOut,
    MarketPulseOut,
    MovementCaseOut,
    PulseMoverOut,
    RawTestOut,
    ReferenceReturnOut,
    SectorReturnOut,
    SymbolMatchOut,
    SymbolSearchOut,
    TickerFilingOut,
    TickerFilingsOut,
)
from app.services import evidence, response_cache, sessions, symbol_directory
from app.services.edgar import EARNINGS_ITEM, ITEM_EVENTS

router = APIRouter(tags=["research"])

# Bars this many calendar days back are enough to find the previous session's close.
PREVIOUS_BAR_LOOKBACK = timedelta(days=10)
SHORT_CACHE = "private, max-age=60"


# ------------------------------------------------------------- symbol search


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/symbols/search", response_model=SymbolSearchOut, summary="Find a ticker by symbol or company name")
async def search_symbols(
    session: SessionDep,
    principal: CurrentUser,
    response: Response,
    q: str = Query(..., min_length=1, max_length=40, description="A symbol prefix or part of a company name."),
    limit: int = Query(10, ge=1, le=25),
) -> SymbolSearchOut:
    """Exact symbol first, then symbol prefixes, then names starting with the
    query, then names with a word starting with it, then names containing it. Searches the US symbol directory and the
    tickers Metrix holds (which covers a deployment whose directory is empty).
    On Postgres a trigram index serves the name match."""
    query = q.strip()
    upper = normalize_symbol(query)
    prefix = _like_escape(upper) + "%"
    lowered = _like_escape(query.lower())
    starts, contains = lowered + "%", "%" + lowered + "%"
    word = "% " + lowered + "%"

    def ranked(symbol_col, name_col):
        name = sa.func.lower(name_col)  # matches the trigram index expression
        return sa.case(
            (symbol_col == upper, 0),
            (symbol_col.like(prefix, escape="\\"), 1),
            (name.like(starts, escape="\\"), 2),
            (name.like(word, escape="\\"), 3),
            else_=4,
        )

    def matching(symbol_col, name_col):
        name = sa.func.lower(name_col)  # matches the trigram index expression
        return sa.or_(symbol_col.like(prefix, escape="\\"), name.like(contains, escape="\\"))

    listed_rank = ranked(ListedSymbol.symbol, ListedSymbol.name)
    listed = (
        await session.execute(
            sa.select(ListedSymbol, listed_rank.label("rank"))
            .where(matching(ListedSymbol.symbol, ListedSymbol.name))
            .order_by(listed_rank, sa.func.length(ListedSymbol.symbol), ListedSymbol.symbol)
            .limit(limit)
        )
    ).all()
    held_rank = ranked(Ticker.symbol, Ticker.company_name)
    held = (
        await session.execute(
            sa.select(Ticker, held_rank.label("rank"))
            .where(matching(Ticker.symbol, Ticker.company_name), Ticker.ingest_error_permanent.is_(False))
            .order_by(held_rank, sa.func.length(Ticker.symbol), Ticker.symbol)
            .limit(limit)
        )
    ).all()

    with_data = {t.symbol: t for t, _ in held if t.last_ingested_at is not None}
    merged: dict[str, tuple[int, SymbolMatchOut]] = {}
    for row, rank in listed:
        merged[row.symbol] = (
            rank,
            SymbolMatchOut(symbol=row.symbol, name=row.name, exchange=row.exchange, is_etf=row.is_etf,
                           has_data=row.symbol in with_data),
        )
    for ticker, rank in held:
        if ticker.symbol not in merged:
            merged[ticker.symbol] = (
                rank,
                SymbolMatchOut(symbol=ticker.symbol, name=ticker.company_name, exchange=ticker.exchange,
                               has_data=ticker.last_ingested_at is not None),
            )
    results = sorted(merged.values(), key=lambda item: (item[0], len(item[1].symbol), item[1].symbol))
    response.headers["Cache-Control"] = "private, max-age=300"
    return SymbolSearchOut(query=query, results=[match for _, match in results[:limit]])


# ----------------------------------------------------------------- helpers


async def _stored_ticker(session: AsyncSession, symbol: str) -> Ticker:
    symbol = normalize_symbol(symbol)
    if not is_valid_symbol(symbol):
        raise HTTPException(status_code=422, detail=f"'{symbol}' is not a valid ticker symbol.")
    symbol = symbol_directory.canonical(symbol)
    ticker = await session.scalar(sa.select(Ticker).where(Ticker.symbol == symbol))
    if ticker is None or ticker.last_ingested_at is None:
        raise HTTPException(
            status_code=404, detail=f"No data for {symbol} yet; GET /tickers/{symbol} fetches it."
        )
    return ticker


async def _session_returns(
    session: AsyncSession, symbols: list[str], day: date, *, sessions_back: int = 1
) -> dict[str, float | None]:
    """Each symbol's compounded close-to-close return over the `sessions_back`
    sessions ending on `day`, from stored bars; None where they are missing."""
    if not symbols:
        return {}
    start = sessions.offset_session(day, -sessions_back) if sessions.is_session(day) else None
    since = (start or day) - PREVIOUS_BAR_LOOKBACK
    rows = (
        await session.execute(
            sa.select(Ticker.symbol, PriceBar.date, PriceBar.adj_close)
            .join(PriceBar, PriceBar.ticker_id == Ticker.id)
            .where(Ticker.symbol.in_(symbols), PriceBar.date >= since, PriceBar.date <= day)
            .order_by(Ticker.symbol, PriceBar.date)
        )
    ).all()
    bars: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for symbol, bar_date, close in rows:
        bars[symbol].append((bar_date, float(close)))
    out: dict[str, float | None] = {}
    for symbol in symbols:
        series = bars.get(symbol, [])
        last = next((close for d, close in reversed(series) if d == day), None)
        base_day = start if start is not None else None
        base = next((close for d, close in reversed(series) if base_day is not None and d <= base_day), None)
        out[symbol] = (last / base - 1.0) if last and base else None
    return out


async def _versions(session: AsyncSession, symbols: list[str]) -> list[int]:
    rows = dict(
        (await session.execute(sa.select(Ticker.symbol, Ticker.data_version).where(Ticker.symbol.in_(symbols)))).all()
    )
    return [rows.get(s, -1) for s in symbols]


# --------------------------------------------------------------- case file


@router.get(
    "/tickers/{symbol}/movements/{day}",
    response_model=MovementCaseOut,
    summary="One movement's case file: decomposition, evidence, peers and the detection audit",
    responses={304: {"description": "Not modified"}, 404: {"description": "No such ticker or movement"}},
)
async def movement_case(symbol: str, day: date, request: Request, session: SessionDep, principal: CurrentUser):
    ticker = await _stored_ticker(session, symbol)
    movement = await session.scalar(
        sa.select(Movement)
        .where(Movement.ticker_id == ticker.id, Movement.date == day)
        .options(
            selectinload(Movement.news_links).selectinload(MovementNewsLink.article),
            selectinload(Movement.filings),
        )
    )
    if movement is None:
        raise HTTPException(status_code=404, detail=f"{ticker.symbol} has no movement on {day.isoformat()}.")

    peer_set = (ticker.peers or {}).get("peers", [])
    peer_symbols = [normalize_symbol(p["ticker"]) for p in peer_set if isinstance(p, dict) and p.get("ticker")]
    bench_symbols = [s for s in (movement.market_benchmark, movement.sector_benchmark) if s]
    now = datetime.now(timezone.utc)
    jobs = await evidence.active_jobs(session, ticker.id)
    etag = response_cache.etag_for(
        {
            "case": [ticker.symbol, day.isoformat()],
            "versions": await _versions(session, [ticker.symbol, *bench_symbols, *peer_symbols]),
            "evidence": evidence.fingerprint({k: v for k, v in jobs.items() if k == movement.id}, now),
        }
    )
    headers = {"ETag": etag, "Cache-Control": "private, no-cache", "Vary": "Authorization"}
    if response_cache.matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)

    returns = await _session_returns(session, bench_symbols + peer_symbols, day)
    neighbours = (
        await session.execute(
            sa.select(
                sa.select(sa.func.max(Movement.date))
                .where(Movement.ticker_id == ticker.id, Movement.qualifies.is_(True), Movement.date < day)
                .scalar_subquery(),
                sa.select(sa.func.min(Movement.date))
                .where(Movement.ticker_id == ticker.id, Movement.qualifies.is_(True), Movement.date > day)
                .scalar_subquery(),
            )
        )
    ).one()

    floor_basis = settings.movement_abnormal_floor_basis
    abnormal_passed = None
    if movement.sar is not None:
        floored = movement.abnormal_return if floor_basis == "abnormal" else movement.daily_return
        abnormal_passed = abs(movement.sar) >= settings.movement_abnormal_k and abs(floored or 0.0) >= movement.detector_floor
    case = MovementCaseOut(
        ticker=TickerOut.model_validate(ticker),
        movement=_movement_out(movement, None, jobs, now),
        detection=DetectionAuditOut(
            trigger=movement.trigger or "raw",
            qualifies=movement.qualifies,
            current_mode=settings.movement_detection_mode,
            raw=RawTestOut(
                daily_return=movement.daily_return,
                threshold=movement.threshold,
                threshold_source=movement.threshold_source,
                rolling_std=movement.rolling_std,
                k=movement.detector_k,
                window=movement.detector_window,
                floor=movement.detector_floor,
                passed=movement.abs_return >= movement.threshold,
            ),
            abnormal=AbnormalTestOut(
                sar=movement.sar,
                abnormal_return=movement.abnormal_return,
                k=settings.movement_abnormal_k,
                floor_basis=floor_basis,
                floor=movement.detector_floor,
                passed=abnormal_passed,
            ),
        ),
        benchmarks=[
            ReferenceReturnOut(symbol=symbol, role=role, daily_return=returns.get(symbol))
            for role, symbol in (("market", movement.market_benchmark), ("sector", movement.sector_benchmark))
            if symbol
        ],
        peers=[
            ReferenceReturnOut(
                symbol=normalize_symbol(p["ticker"]) if p.get("ticker") else None,
                name=p.get("name"),
                role=p.get("relationship", "peer"),
                daily_return=returns.get(normalize_symbol(p["ticker"])) if p.get("ticker") else None,
            )
            for p in peer_set
            if isinstance(p, dict)
        ],
        previous_date=neighbours[0],
        next_date=neighbours[1],
    )
    return Response(content=case.model_dump_json(), media_type="application/json", headers=headers)


# ----------------------------------------------------------------- filings


@router.get("/tickers/{symbol}/filings", response_model=TickerFilingsOut, summary="A ticker's stored SEC filings")
async def ticker_filings(
    symbol: str,
    session: SessionDep,
    principal: CurrentUser,
    response: Response,
    form: list[str] | None = Query(None, description="Forms to include (8-K, 10-Q, ...). Repeat for several."),
    item: list[str] | None = Query(None, description="8-K items to include (2.02, 5.02, ...)."),
    start: date | None = Query(None),
    end: date | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> TickerFilingsOut:
    ticker = await _stored_ticker(session, symbol)
    conditions = [Filing.ticker_id == ticker.id]
    if form:
        conditions.append(Filing.form.in_([f.upper() for f in form]))
    if start:
        conditions.append(Filing.filing_date >= start)
    if end:
        conditions.append(Filing.filing_date <= end)
    rows = (
        await session.scalars(
            sa.select(Filing).where(*conditions).order_by(Filing.filing_date.desc(), Filing.accession.desc())
        )
    ).all()
    if item:
        wanted = set(item)
        rows = [f for f in rows if wanted & set(f.items or [])]
    rows = rows[:limit]
    attached: dict[int, list[date]] = defaultdict(list)
    if rows:
        for filing_id, movement_date in (
            await session.execute(
                sa.select(MovementFiling.filing_id, Movement.date)
                .join(Movement, Movement.id == MovementFiling.movement_id)
                .where(MovementFiling.filing_id.in_([f.id for f in rows]))
                .order_by(Movement.date)
            )
        ).all():
            attached[filing_id].append(movement_date)
    response.headers["Cache-Control"] = SHORT_CACHE
    return TickerFilingsOut(
        symbol=ticker.symbol,
        filings=[
            TickerFilingOut(
                accession=f.accession, form=f.form, items=list(f.items or []),
                events=[ITEM_EVENTS[i] for i in (f.items or []) if i in ITEM_EVENTS],
                filing_date=f.filing_date, accepted_at=f.accepted_at, url=f.url,
                movement_dates=attached.get(f.id, []),
            )
            for f in rows
        ],
        note=None if settings.sec_edgar_enabled else "SEC EDGAR is not enabled here (SEC_EDGAR_ENABLED); "
        "only filings stored earlier are shown.",
    )


# ---------------------------------------------------------------- earnings


@router.get("/tickers/{symbol}/earnings", response_model=EarningsOut, summary="Earnings releases and the moves after them")
async def ticker_earnings(symbol: str, session: SessionDep, principal: CurrentUser, response: Response) -> EarningsOut:
    """From 8-K filings with Item 2.02 (results of operations), each placed on
    the first session whose prices could reflect it, with the movement on that
    session when the day was flagged. No estimates or surprises yet."""
    ticker = await _stored_ticker(session, symbol)
    filings = [
        f
        for f in (
            await session.scalars(
                sa.select(Filing).where(Filing.ticker_id == ticker.id, Filing.form.like("8-K%"))
                .order_by(Filing.filing_date.desc())
            )
        ).all()
        if EARNINGS_ITEM in (f.items or [])
    ]
    placed = [(f, *sessions.first_affected_session(f.accepted_at, f.filing_date)) for f in filings]
    days = [day for _, day, _ in placed if day is not None]
    movements = {
        m.date: m
        for m in (
            await session.scalars(
                sa.select(Movement).where(Movement.ticker_id == ticker.id, Movement.date.in_(days),
                                          Movement.qualifies.is_(True))
            )
        ).all()
    } if days else {}
    now = datetime.now(timezone.utc)
    events = []
    for filing, day, timing in placed:
        movement = movements.get(day)
        events.append(
            EarningsEventOut(
                accession=filing.accession, accepted_at=filing.accepted_at, filing_date=filing.filing_date,
                session=day, timing=timing, url=filing.url,
                movement=EarningsMovementOut(
                    date=movement.date, daily_return=movement.daily_return,
                    abnormal_return=movement.abnormal_return,
                    evidence=evidence.status_for(movement, has_evidence=False, job=None, now=now).state
                    if movement.news_status is not None else None,
                ) if movement else None,
            )
        )
    response.headers["Cache-Control"] = SHORT_CACHE
    return EarningsOut(
        symbol=ticker.symbol,
        events=events,
        note="Derived from SEC 8-K Item 2.02 filings; estimates, surprises and upcoming dates are not "
        "available yet." + ("" if settings.sec_edgar_enabled else " SEC EDGAR is not enabled here."),
    )


# ------------------------------------------------------------------- pulse


@router.get("/market/pulse", response_model=MarketPulseOut, summary="The largest abnormal moves and sector returns")
async def market_pulse(
    session: SessionDep,
    principal: CurrentUser,
    response: Response,
    sessions_back: int = Query(1, alias="sessions", ge=1, le=10, description="How many recent sessions."),
    limit: int = Query(10, ge=1, le=50),
) -> MarketPulseOut:
    """Across every ticker Metrix holds (the pre-warmed universe, plus anything
    requested): the latest sessions' movements, largest standardised abnormal
    return first, each with its strongest evidence in a line."""
    market = settings.benchmark_market_symbol.strip().upper()
    latest = await session.scalar(
        sa.select(sa.func.max(PriceBar.date)).join(Ticker, Ticker.id == PriceBar.ticker_id).where(Ticker.symbol == market)
    ) or await session.scalar(sa.select(sa.func.max(Movement.date)))
    if latest is None:
        return MarketPulseOut(sessions=[], movers=[], sectors=[])
    first = sessions.offset_session(latest, -(sessions_back - 1)) if sessions.is_session(latest) else latest
    first = first or latest
    covered = [first]
    while covered[-1] < latest:
        following = sessions.offset_session(covered[-1], 1)
        if following is None or following > latest:
            break
        covered.append(following)

    movers = (
        await session.scalars(
            sa.select(Movement)
            .join(Ticker, Ticker.id == Movement.ticker_id)
            .where(Movement.date >= first, Movement.date <= latest, Movement.qualifies.is_(True),
                   Ticker.is_benchmark.is_(False))
            .order_by(sa.func.abs(sa.func.coalesce(Movement.sar, 0.0)).desc(), Movement.abs_return.desc())
            .limit(limit)
            .options(
                selectinload(Movement.news_links).selectinload(MovementNewsLink.article),
                selectinload(Movement.filings),
                selectinload(Movement.ticker),
            )
        )
    ).all()
    now = datetime.now(timezone.utc)
    out_movers = []
    for m in movers:
        shaped = _movement_out(m, None, None, now)
        top = shaped.news[0] if shaped.news else None
        if top is not None:
            explanation = f"{top.article.title or top.article.url}: {top.rationale}" if top.rationale else (top.article.title or top.article.url)
        else:
            explanation = shaped.evidence.detail if shaped.evidence else "No evidence yet."
        out_movers.append(
            PulseMoverOut(
                symbol=m.ticker.symbol, company_name=m.ticker.company_name, date=m.date,
                daily_return=m.daily_return, abnormal_return=m.abnormal_return, sar=m.sar,
                dominant=m.dominant_component, explanation=explanation,
                evidence=shaped.evidence.state if shaped.evidence else "not_started",
            )
        )

    sector_map = settings.benchmark_sector_map_json
    labels = [("Market", market)] + sorted((name, symbol) for name, symbol in sector_map.items())
    returns = await _session_returns(session, [symbol for _, symbol in labels], latest, sessions_back=sessions_back)
    response.headers["Cache-Control"] = SHORT_CACHE
    return MarketPulseOut(
        sessions=covered,
        movers=out_movers,
        sectors=[SectorReturnOut(sector=name, symbol=symbol, total_return=returns.get(symbol)) for name, symbol in labels],
    )



