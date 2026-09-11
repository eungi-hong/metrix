"""GET /tickers/{symbol} -- stock and news data, with filters.

The route stays thin on purpose: validate and normalize input, decide whether
ingestion is owed, run the query, shape the response. Every decision with real
logic in it lives in `app.services.ingestion`.
"""

from __future__ import annotations

import re
from datetime import date

import sqlalchemy as sa
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import LLMDep, SessionDep
from app.core.logging import get_logger
from app.models.enums import Direction, IngestStatus, RelevanceTier
from app.models.market import Movement, PriceBar, Ticker
from app.models.news import MovementNewsLink
from app.schemas.market import (
    AppliedFiltersOut,
    ArticleOut,
    IngestState,
    LinkedArticleOut,
    MovementOut,
    PaginationOut,
    PriceBarOut,
    PriceRangeOut,
    TickerDetailOut,
    TickerOut,
)
from app.services import ingestion
from app.services.llm import LLMClient
from app.services.movements import sigma_multiple

logger = get_logger(__name__)
router = APIRouter(tags=["tickers"])

# Covers ordinary symbols plus class shares and foreign listings (BRK.B, RY.TO).
SYMBOL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,11}$")


@router.get(
    "/tickers/{symbol}",
    response_model=TickerDetailOut,
    summary="Stock movements for a ticker, each with the news that explains it",
)
async def get_ticker_detail(
    symbol: str,
    session: SessionDep,
    llm: LLMDep,
    background: BackgroundTasks,
    response: Response,
    start: date | None = Query(None, description="Only movements on or after this date."),
    end: date | None = Query(None, description="Only movements on or before this date."),
    min_magnitude_pct: float | None = Query(
        None,
        ge=0,
        le=100,
        description="Only movements at least this large, in percent (e.g. 5 = 5%).",
    ),
    direction: Direction | None = Query(None, description="Filter to up or down days."),
    tier: list[RelevanceTier] | None = Query(
        None,
        description="Relevance tier(s) to include. Repeat the parameter for several. "
        "Movements with no news in these tiers are excluded.",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    include_prices: bool = Query(
        False,
        description="Include the daily OHLCV bars themselves, not just a summary. "
        "Honours `start`/`end`. Off by default because a year of bars is ~250 rows "
        "that most callers do not need.",
    ),
    refresh: bool = Query(False, description="Force re-ingestion even if data is fresh."),
    wait: bool = Query(
        False,
        description="Run a needed ingestion inline and return the finished data. "
        "Slower (it calls yfinance, the news API and the LLM), but convenient for "
        "a first request against a cold ticker.",
    ),
) -> TickerDetailOut:
    symbol = symbol.strip().upper()
    if not SYMBOL_PATTERN.match(symbol):
        raise HTTPException(
            status_code=422,
            detail=f"'{symbol}' is not a valid ticker symbol.",
        )
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="`start` must be on or before `end`.")

    ticker = await ingestion.get_ticker(session, symbol)
    state, message, warnings = await _ensure_data(
        session, ticker, symbol, background, llm, refresh=refresh, wait=wait
    )

    ticker = await ingestion.get_ticker(session, symbol)
    if ticker is None:
        # Only reachable when a background ingestion has not yet created the row.
        response.status_code = 202
        return _empty_response(symbol, state, message)

    conditions: list[sa.ColumnElement[bool]] = [Movement.ticker_id == ticker.id]
    if start:
        conditions.append(Movement.date >= start)
    if end:
        conditions.append(Movement.date <= end)
    if min_magnitude_pct is not None:
        conditions.append(Movement.abs_return >= min_magnitude_pct / 100.0)
    if direction is not None:
        conditions.append(Movement.direction == direction)
    if tier:
        conditions.append(
            Movement.id.in_(
                sa.select(MovementNewsLink.movement_id).where(
                    MovementNewsLink.relevance_tier.in_(tier)
                )
            )
        )

    total = await session.scalar(
        sa.select(sa.func.count()).select_from(Movement).where(*conditions)
    )
    movements = (
        await session.scalars(
            sa.select(Movement)
            .where(*conditions)
            .order_by(Movement.date.desc())
            .limit(limit)
            .offset(offset)
            .options(
                selectinload(Movement.news_links).selectinload(MovementNewsLink.article)
            )
        )
    ).all()

    if state == "ingesting":
        response.status_code = 202
    elif state == "failed" and not movements:
        # Nothing stored and the last attempt failed: this is an upstream
        # problem, not an empty-but-valid result.
        response.status_code = 502

    return TickerDetailOut(
        status=state,
        message=message,
        ticker=TickerOut.model_validate(ticker),
        ingest_status=ticker.ingest_status,
        last_ingested_at=ticker.last_ingested_at,
        price_range=await _price_range(session, ticker),
        prices=(
            await _price_bars(session, ticker, start, end) if include_prices else None
        ),
        filters=AppliedFiltersOut(
            start=start,
            end=end,
            min_magnitude=min_magnitude_pct,
            direction=direction,
            tiers=list(tier) if tier else None,
        ),
        pagination=PaginationOut(
            limit=limit, offset=offset, total=total or 0, returned=len(movements)
        ),
        movements=[_movement_out(m, tier) for m in movements],
        warnings=warnings,
    )


async def _ensure_data(
    session: AsyncSession,
    ticker: Ticker | None,
    symbol: str,
    background: BackgroundTasks,
    llm: LLMClient,
    *,
    refresh: bool,
    wait: bool,
) -> tuple[IngestState, str | None, list[str]]:
    """Fetch-if-missing / fetch-if-stale. Returns the state to report."""
    has_data = ticker is not None and ticker.last_ingested_at is not None
    if ticker is not None and not refresh and not ingestion.is_stale(ticker):
        return "ready", None, []

    # Report a recent failure instead of silently retrying it on every request.
    # Without this a permanently broken symbol (delisted, or a news key that is
    # not valid) reports "ingesting" forever to a polling client and re-runs the
    # whole pipeline each time it is asked. `refresh=true` forces a retry.
    if ticker is not None and not refresh and ingestion.failed_recently(ticker):
        return (
            "failed",
            ticker.ingest_error or "The last ingestion for this ticker failed.",
            [],
        )

    if ticker is not None and ticker.ingest_status == IngestStatus.RUNNING and not wait:
        return (
            ("refreshing" if has_data else "ingesting"),
            "An ingestion is already running for this ticker.",
            [],
        )

    target = ticker or await ingestion.get_or_create_ticker(session, symbol)
    claimed = await ingestion.claim_ingestion(session, target)

    if not claimed:
        return (
            ("refreshing" if has_data else "ingesting"),
            "An ingestion is already running for this ticker.",
            [],
        )

    if wait:
        result = await ingestion.ingest_ticker(session, symbol, llm=llm)
        return "ready", None, result.warnings

    background.add_task(ingestion.run_ingestion_in_background, symbol)
    if has_data:
        return (
            "refreshing",
            "Showing stored data; a refresh is running in the background.",
            [],
        )
    return (
        "ingesting",
        "First-time ingestion started. Poll this endpoint, or repeat the request "
        "with `?wait=true` to block until it finishes.",
        [],
    )


def _movement_out(
    movement: Movement, tiers: list[RelevanceTier] | None
) -> MovementOut:
    links = sorted(movement.news_links, key=lambda l: l.relevance_score, reverse=True)
    if tiers:
        links = [link for link in links if link.relevance_tier in tiers]

    return MovementOut(
        id=movement.id,
        date=movement.date,
        daily_return=movement.daily_return,
        daily_return_pct=round(movement.daily_return * 100, 4),
        direction=movement.direction,
        prev_adj_close=float(movement.prev_adj_close),
        adj_close=float(movement.adj_close),
        volume=movement.volume,
        threshold=movement.threshold,
        threshold_source=movement.threshold_source,
        rolling_std=movement.rolling_std,
        sigma_multiple=sigma_multiple(movement.daily_return, movement.rolling_std),
        detector_k=movement.detector_k,
        detector_window=movement.detector_window,
        detector_floor=movement.detector_floor,
        news_status=movement.news_status,
        news=[
            LinkedArticleOut(
                article=ArticleOut(
                    id=link.article.id,
                    url=link.article.url,
                    title=link.article.title,
                    source=link.article.source_domain,
                    author=link.article.author,
                    published_at=link.article.published_at,
                    summary=link.article.summary,
                ),
                relevance_tier=link.relevance_tier,
                relevance_score=link.relevance_score,
                rationale=link.rationale,
                search_tier=link.search_tier,
            )
            for link in links
        ],
    )


async def _price_range(session: AsyncSession, ticker: Ticker) -> PriceRangeOut:
    row = (
        await session.execute(
            sa.select(
                sa.func.min(PriceBar.date),
                sa.func.max(PriceBar.date),
                sa.func.count(PriceBar.id),
            ).where(PriceBar.ticker_id == ticker.id)
        )
    ).one()
    return PriceRangeOut(start=row[0], end=row[1], bars=row[2] or 0)


async def _price_bars(
    session: AsyncSession, ticker: Ticker, start: date | None, end: date | None
) -> list[PriceBarOut]:
    """The daily bars for a ticker, honouring the same date filters as movements."""
    conditions: list[sa.ColumnElement[bool]] = [PriceBar.ticker_id == ticker.id]
    if start:
        conditions.append(PriceBar.date >= start)
    if end:
        conditions.append(PriceBar.date <= end)

    bars = (
        await session.scalars(
            sa.select(PriceBar).where(*conditions).order_by(PriceBar.date)
        )
    ).all()
    return [
        PriceBarOut(
            date=bar.date,
            open=_as_float(bar.open),
            high=_as_float(bar.high),
            low=_as_float(bar.low),
            close=_as_float(bar.close),
            adj_close=float(bar.adj_close),
            volume=bar.volume,
        )
        for bar in bars
    ]


def _as_float(value: object | None) -> float | None:
    """Prices are stored as NUMERIC; JSON wants a number, not a Decimal string."""
    return None if value is None else float(value)


def _empty_response(symbol: str, state: IngestState, message: str | None) -> TickerDetailOut:
    return TickerDetailOut(
        status=state,
        message=message,
        ticker=TickerOut(
            symbol=symbol,
            company_name=None,
            sector=None,
            industry=None,
            exchange=None,
            currency=None,
        ),
        ingest_status=IngestStatus.RUNNING,
        last_ingested_at=None,
        price_range=PriceRangeOut(start=None, end=None, bars=0),
        filters=AppliedFiltersOut(),
        pagination=PaginationOut(limit=0, offset=0, total=0, returned=0),
        movements=[],
    )
