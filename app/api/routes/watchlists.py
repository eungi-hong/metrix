"""Watchlists: CRUD for the caller's own lists, and a digest of each.

A list belongs to whoever created it (the signed-in user, or with
AUTH_REQUIRED off the anonymous caller's IP); anyone else gets 404, so the
existence of another caller's list is not revealed. The digest reads stored
data only: holdings Metrix has not fetched are listed as missing, never
fetched here.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, SessionDep
from app.api.routes.tickers import _movement_out
from app.core.config import settings
from app.core.symbols import is_valid_symbol, normalize_symbol
from app.models.market import Movement, PriceBar, Ticker
from app.models.news import MovementNewsLink
from app.models.watchlists import Watchlist, WatchlistHolding
from app.schemas.watchlists import (
    DigestHoldingOut,
    DigestMoveOut,
    HoldingOut,
    WatchlistDigestOut,
    WatchlistIn,
    WatchlistOut,
)
from app.services import returns, sessions, symbol_directory
from app.services.auth import Principal

router = APIRouter(tags=["watchlists"])

MAX_WATCHLISTS = 20


def _owned_by(principal: Principal) -> sa.ColumnElement[bool]:
    if principal.user_id is not None:
        return Watchlist.user_id == principal.user_id
    return sa.and_(Watchlist.user_id.is_(None), Watchlist.anonymous_key == principal.key)


async def get_owned(session: AsyncSession, principal: Principal, watchlist_id: int) -> Watchlist:
    watchlist = await session.scalar(sa.select(Watchlist).where(Watchlist.id == watchlist_id, _owned_by(principal)))
    if watchlist is None:
        raise HTTPException(status_code=404, detail="No such watchlist.")
    return watchlist


def _weights(holdings: list[WatchlistHolding]) -> dict[str, float]:
    if not holdings:
        return {}
    if all(h.weight is not None for h in holdings):
        total = sum(h.weight for h in holdings)
        return {h.symbol: h.weight / total for h in holdings}
    return {h.symbol: 1.0 / len(holdings) for h in holdings}


def _out(watchlist: Watchlist) -> WatchlistOut:
    weights = _weights(watchlist.holdings)
    return WatchlistOut(
        id=watchlist.id,
        name=watchlist.name,
        weighted=bool(watchlist.holdings) and all(h.weight is not None for h in watchlist.holdings),
        holdings=[HoldingOut(symbol=h.symbol, weight=weights[h.symbol]) for h in watchlist.holdings],
        created_at=watchlist.created_at,
        updated_at=watchlist.updated_at,
    )


async def _holdings(session: AsyncSession, body: WatchlistIn) -> list[WatchlistHolding]:
    """Validated, canonical and de-duplicated; no external calls."""
    seen: dict[str, WatchlistHolding] = {}
    for position, item in enumerate(body.holdings):
        symbol = normalize_symbol(item.symbol)
        if not is_valid_symbol(symbol):
            raise HTTPException(status_code=422, detail=f"'{item.symbol}' is not a valid ticker symbol.")
        verdict = await symbol_directory.check(session, symbol)
        if not verdict.allowed:
            raise HTTPException(status_code=422, detail=f"'{symbol}' is not a listed US symbol.")
        if verdict.symbol in seen:
            raise HTTPException(status_code=422, detail=f"'{verdict.symbol}' is listed twice.")
        seen[verdict.symbol] = WatchlistHolding(symbol=verdict.symbol, weight=item.weight, position=position)
    return list(seen.values())


@router.get("/watchlists", response_model=list[WatchlistOut], summary="Your watchlists")
async def list_watchlists(session: SessionDep, principal: CurrentUser) -> list[WatchlistOut]:
    rows = (await session.scalars(sa.select(Watchlist).where(_owned_by(principal)).order_by(Watchlist.id))).all()
    return [_out(w) for w in rows]


@router.post("/watchlists", response_model=WatchlistOut, status_code=201, summary="Create a watchlist")
async def create_watchlist(body: WatchlistIn, session: SessionDep, principal: CurrentUser) -> WatchlistOut:
    count = await session.scalar(sa.select(sa.func.count()).select_from(Watchlist).where(_owned_by(principal)))
    if (count or 0) >= MAX_WATCHLISTS:
        raise HTTPException(status_code=409, detail=f"At most {MAX_WATCHLISTS} watchlists each.")
    watchlist = Watchlist(
        name=body.name,
        user_id=principal.user_id,
        anonymous_key=None if principal.user_id is not None else principal.key,
        holdings=await _holdings(session, body),
    )
    session.add(watchlist)
    await session.commit()
    await session.refresh(watchlist)
    return _out(watchlist)


@router.get("/watchlists/{watchlist_id}", response_model=WatchlistOut, summary="One watchlist")
async def get_watchlist(watchlist_id: int, session: SessionDep, principal: CurrentUser) -> WatchlistOut:
    return _out(await get_owned(session, principal, watchlist_id))


@router.put("/watchlists/{watchlist_id}", response_model=WatchlistOut, summary="Replace a watchlist's name and holdings")
async def replace_watchlist(
    watchlist_id: int, body: WatchlistIn, session: SessionDep, principal: CurrentUser
) -> WatchlistOut:
    watchlist = await get_owned(session, principal, watchlist_id)
    holdings = await _holdings(session, body)
    watchlist.name = body.name
    watchlist.holdings.clear()
    await session.flush()  # the old rows go before the new ones meet the unique constraint
    watchlist.holdings.extend(holdings)
    watchlist.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(watchlist)
    return _out(watchlist)


@router.delete("/watchlists/{watchlist_id}", status_code=204, summary="Delete a watchlist")
async def delete_watchlist(watchlist_id: int, session: SessionDep, principal: CurrentUser) -> Response:
    await session.delete(await get_owned(session, principal, watchlist_id))
    await session.commit()
    return Response(status_code=204)


@router.get(
    "/watchlists/{watchlist_id}/digest",
    response_model=WatchlistDigestOut,
    summary="Each holding's moves and contribution over the last sessions",
)
async def watchlist_digest(
    watchlist_id: int,
    session: SessionDep,
    principal: CurrentUser,
    response: Response,
    sessions_back: int = Query(5, alias="sessions", ge=1, le=60),
) -> WatchlistDigestOut:
    watchlist = await get_owned(session, principal, watchlist_id)
    response.headers["Cache-Control"] = "private, max-age=60"
    return await build_digest(session, watchlist, sessions_back)


async def build_digest(session: AsyncSession, watchlist: Watchlist, sessions_back: int) -> WatchlistDigestOut:
    """The digest of one watchlist (ownership already checked). Also used by
    Ask Metrix's get_watchlist_digest tool."""
    symbols = [h.symbol for h in watchlist.holdings]
    tickers = {
        t.symbol: t
        for t in (await session.scalars(sa.select(Ticker).where(Ticker.symbol.in_(symbols)))).all()
    } if symbols else {}
    market = settings.benchmark_market_symbol.strip().upper()
    end = await session.scalar(
        sa.select(sa.func.max(PriceBar.date)).join(Ticker, Ticker.id == PriceBar.ticker_id)
        .where(Ticker.symbol.in_([market, *symbols]))
    ) if symbols else None
    if end is None:
        return WatchlistDigestOut(id=watchlist.id, name=watchlist.name, sessions=[], period_return=None,
                                  holdings=[], missing=symbols)

    covered: list[date] = [end]
    if sessions.is_session(end):
        while len(covered) < sessions_back:
            earlier = sessions.offset_session(covered[0], -1)
            if earlier is None:
                break
            covered.insert(0, earlier)
    period = await returns.session_returns(session, symbols, end, sessions_back=sessions_back)
    with_data = [s for s in symbols if period.get(s) is not None]
    weights = _weights([h for h in watchlist.holdings if h.symbol in with_data])

    movers = (
        await session.scalars(
            sa.select(Movement)
            .where(Movement.ticker_id.in_([t.id for t in tickers.values()]), Movement.qualifies.is_(True),
                   Movement.date >= covered[0], Movement.date <= end)
            .order_by(Movement.date.desc())
            .options(selectinload(Movement.news_links).selectinload(MovementNewsLink.article),
                     selectinload(Movement.filings))
        )
    ).all() if tickers else []
    by_ticker: dict[int, list[Movement]] = {}
    for m in movers:
        by_ticker.setdefault(m.ticker_id, []).append(m)

    now = datetime.now(timezone.utc)
    holdings_out = []
    for symbol in symbols:
        ticker = tickers.get(symbol)
        moves = []
        for m in by_ticker.get(ticker.id, []) if ticker else []:
            shaped = _movement_out(m, None, None, now)
            top = shaped.news[0] if shaped.news else None
            moves.append(
                DigestMoveOut(
                    date=m.date, daily_return=m.daily_return, abnormal_return=m.abnormal_return,
                    evidence=shaped.evidence.state,
                    explanation=(top.article.title or top.article.url) if top else shaped.evidence.detail,
                )
            )
        r = period.get(symbol)
        weight = weights.get(symbol)
        holdings_out.append(
            DigestHoldingOut(
                symbol=symbol,
                weight=weight if weight is not None else 0.0,
                has_data=ticker is not None and ticker.last_ingested_at is not None,
                period_return=r,
                contribution=(weight * r) if (weight is not None and r is not None) else None,
                moves=moves,
            )
        )
    contributions = [h.contribution for h in holdings_out if h.contribution is not None]
    return WatchlistDigestOut(
        id=watchlist.id,
        name=watchlist.name,
        sessions=covered,
        period_return=sum(contributions) if contributions else None,
        holdings=holdings_out,
        missing=[s for s in symbols if s not in with_data],
    )
