"""GET /tickers/{symbol} -- stock and news data, with filters.

The route stays thin on purpose: validate and normalize input, decide whether
ingestion is owed, run the query, shape the response. Every decision with real
logic in it lives in `app.services.ingestion`.

Ingestion the caller does not wait for is enqueued as an `ingest_ticker` job
at interactive priority, not run in-process: it survives a restart, is
retried on transient failure, and goes through the same rate-limited workers
as everything else. The job's id is returned so the caller can follow it at
`GET /jobs/{id}`.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, LLMDep, SessionDep
from app.core.config import settings
from app.core.context import CallClass
from app.core.errors import (
    MetrixError,
    ProviderBusy,
    RateLimited,
    SymbolNotListed,
    UpstreamError,
    WorkDeferred,
)
from app.core.logging import get_logger
from app.core.symbols import is_valid_symbol, normalize_symbol
from app.models.enums import Direction, IngestStatus, RelevanceTier
from app.models.jobs import JobKind, JobSource
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
from app.services import demand, ingestion, queue, quotas, spend, symbol_directory
from app.services.auth import Principal
from app.services.limits import LimitResult
from app.services.llm import LLMProvider
from app.services.movements import sigma_multiple
from app.services.quotas import Quota

logger = get_logger(__name__)
router = APIRouter(tags=["tickers"])

@router.get(
    "/tickers/{symbol}",
    response_model=TickerDetailOut,
    summary="Stock movements for a ticker, each with the news that explains it",
)
async def get_ticker_detail(
    symbol: str,
    session: SessionDep,
    llm: LLMDep,
    principal: CurrentUser,
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
    symbol = normalize_symbol(symbol)
    if not is_valid_symbol(symbol):
        raise HTTPException(
            status_code=422,
            detail=f"'{symbol}' is not a valid ticker symbol.",
        )
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="`start` must be on or before `end`.")

    # Before anything that could cost money: a symbol that is not listed is
    # refused here, with no external call and no demand recorded.
    verdict = await symbol_directory.check(session, symbol)
    if not verdict.allowed:
        raise SymbolNotListed(symbol)
    symbol = verdict.symbol  # BRK.B is served as BRK-B

    ticker = await ingestion.get_ticker(session, symbol)
    try:
        ensured = await _ensure_data(
            session, ticker, symbol, llm, principal, refresh=refresh, wait=wait
        )
    except RateLimited:
        raise  # a refused request is not demand
    except Exception:
        # A request for a ticker that turns out to be broken is still demand.
        await session.rollback()
        await _count_demand(session, symbol, principal)
        raise
    await _count_demand(session, symbol, principal)
    if verdict.reason == "unlisted":
        ensured.warnings.append(
            f"'{symbol}' is not in the US symbol directory; it was served anyway "
            "because the directory is not enforced."
        )
    state = ensured.state
    if ensured.job_id is not None and await spend.refused_today(session, CallClass.INTERACTIVE):
        ensured.warnings.append(_SPEND_CAP_WARNING)

    ticker = await ingestion.get_ticker(session, symbol)
    if ticker is None:
        # Defensive: `_ensure_data` creates the row before enqueueing.
        response.status_code = 202
        return _empty_response(symbol, state, ensured.message, ensured.job_id)

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
        message=ensured.message,
        job_id=ensured.job_id,
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
        warnings=ensured.warnings,
    )


async def _count_demand(session: AsyncSession, symbol: str, principal: Principal) -> None:
    """Best-effort, and committed at once, so it does not depend on the rest."""
    await demand.count_request(session, symbol, principal)
    await session.commit()


@dataclass(slots=True)
class _Ensured:
    state: IngestState
    message: str | None = None
    job_id: int | None = None
    warnings: list[str] = field(default_factory=list)


_ALREADY_RUNNING = "An ingestion is already running for this ticker."
_SPEND_CAP_WARNING = (
    "Today's spend cap is reached: queued news searches will run after 00:00 UTC. "
    "Stored data is shown."
)
_NO_WAIT_ON_PLAN = (
    "wait=true is not available on the {plan} plan, so the work was queued instead."
)
# Retry-After for a wait=true request turned away because the API's inline
# slots are all busy: inline runs take tens of seconds, so a few is enough.
INLINE_BUSY_RETRY_SECONDS = 5.0

_inline_slots: tuple[int, asyncio.Semaphore] | None = None


def _inline_semaphore() -> asyncio.Semaphore:
    """The process's slots for inline work, sized by API_MAX_INLINE_INGESTIONS."""
    global _inline_slots
    size = settings.api_max_inline_ingestions
    if _inline_slots is None or _inline_slots[0] != size:
        _inline_slots = (size, asyncio.Semaphore(size))
    return _inline_slots[1]


@asynccontextmanager
async def _inline_slot() -> AsyncIterator[None]:
    """Hold one of the API's inline slots, or 429 at once if none is free.

    Refusing rather than queueing for a slot is the point: requests waiting
    on inline work would tie up the API exactly as running it would.
    """
    slots = _inline_semaphore()
    if slots.locked():
        now = datetime.now(timezone.utc)
        busy = LimitResult(
            allowed=False,
            limit=settings.api_max_inline_ingestions,
            remaining=0,
            reset_at=now + timedelta(seconds=INLINE_BUSY_RETRY_SECONDS),
            retry_after=INLINE_BUSY_RETRY_SECONDS,
        )
        raise RateLimited(
            "inline_ingestions",
            busy,
            "Every slot for wait=true work is busy. Retry shortly, or omit wait=true "
            "to have the work queued.",
        )
    async with slots:
        yield


async def _charge(principal: Principal, quota: Quota, *, has_data: bool) -> str | None:
    """Charge `quota` for new work. None if charged.

    Refused, it raises `RateLimited` (429) when there is nothing stored to
    show, or when the caller explicitly asked for a refresh. Otherwise, for a
    stale ticker or news still owed, it returns a warning and the caller
    serves what is stored: the data is still worth more than a 429.
    """
    try:
        await quotas.charge(principal, quota)
    except RateLimited as exc:
        if has_data and quota == Quota.COLD_INGESTS_PER_DAY:
            return f"Showing stored data; nothing new was fetched: {exc}"
        raise
    return None


async def _ensure_data(
    session: AsyncSession,
    ticker: Ticker | None,
    symbol: str,
    llm: LLMProvider,
    principal: Principal,
    *,
    refresh: bool,
    wait: bool,
) -> _Ensured:
    """Fetch-if-missing / fetch-if-stale. Returns the state to report.

    New work is charged to the caller's quota: `refresh_per_day` for an
    explicit refresh of stored data, `cold_ingests_per_day` otherwise. A
    request that joins work already queued is not charged.
    """
    has_data = ticker is not None and ticker.last_ingested_at is not None
    busy: IngestState = "refreshing" if has_data else "ingesting"
    warnings: list[str] = []
    if refresh and ticker is not None and ticker.last_ingested_at is not None:
        # One ticker, refreshed at most once per cooldown, whoever asks: a
        # refresh minutes after the last finds the same news at the same price.
        since = datetime.now(timezone.utc) - _as_utc(ticker.last_ingested_at)
        cooldown = timedelta(minutes=settings.refresh_cooldown_minutes)
        if since < cooldown:
            refresh = False
            warnings.append(
                f"Refreshed {int(since.total_seconds() // 60)} minute(s) ago; refresh=true "
                f"is available again in {math.ceil((cooldown - since).total_seconds() / 60)} "
                "minute(s). Showing stored data."
            )
    if wait and not quotas.limits_for(principal).allow_wait:
        wait = False
        warnings.append(_NO_WAIT_ON_PLAN.format(plan=principal.plan.value))

    if ticker is not None and not refresh and not ingestion.is_stale(ticker):
        ensured = await _enrich_what_is_owed(session, ticker, llm, principal, wait=wait)
        ensured.warnings[:0] = warnings
        return ensured

    # Report a recent failure instead of silently retrying it on every request.
    # Without this a permanently broken symbol (delisted, or a news key that is
    # not valid) reports "ingesting" forever to a polling client and re-runs the
    # whole pipeline each time it is asked. `refresh=true` forces a retry.
    if ticker is not None and not refresh and ingestion.failed_recently(ticker):
        return _Ensured(
            "failed",
            ticker.ingest_error or "The last ingestion for this ticker failed.",
            warnings=warnings,
        )

    if ticker is not None and ticker.ingest_status == IngestStatus.RUNNING and not wait:
        job = await queue.active_job(session, queue.ingest_key(symbol))
        if job is None:
            return _Ensured(busy, _ALREADY_RUNNING, warnings=warnings)
        # Handed its id, so allowed to follow it.
        await queue.add_requester(session, job.id, principal.key)
        await session.commit()
        return _Ensured(busy, _ALREADY_RUNNING, job.id, warnings=warnings)

    target = ticker or await ingestion.get_or_create_ticker(session, symbol)
    quota = Quota.REFRESH_PER_DAY if refresh and has_data else Quota.COLD_INGESTS_PER_DAY

    if wait:
        async with _inline_slot():
            if (refused := await _charge(principal, quota, has_data=has_data)) is not None:
                return _Ensured("ready", warnings=[*warnings, refused])
            if not await ingestion.claim_ingestion(session, target):
                await quotas.refund(principal, quota)
                return _Ensured(busy, _ALREADY_RUNNING, warnings=warnings)
            try:
                result = await ingestion.ingest_ticker(
                    session, symbol, llm=llm, retry_exhausted=refresh
                )
            except (UpstreamError, ProviderBusy):
                await quotas.refund(principal, quota)  # our outage, not their request
                raise
        # A run whose news had to wait (the spend cap, a busy provider) still
        # stores the prices and returns normally; its warnings say which news
        # was not fetched, and the quota is returned.
        if result.deferred is not None:
            await quotas.refund(principal, quota)
        return _Ensured("ready", warnings=[*warnings, *result.warnings])

    # The worker takes the ticker claim when it runs the job, not here: a job
    # can sit in the queue longer than a claim stays valid. If this ticker is
    # already queued, `enqueue` returns that job and pulls it to the front,
    # and joining it is free.
    charged = False
    if await queue.active_job(session, queue.ingest_key(symbol)) is None:
        if (refused := await _charge(principal, quota, has_data=has_data)) is not None:
            return _Ensured("ready", warnings=[*warnings, refused])
        charged = True
    job, created = await queue.enqueue_checked(
        session,
        JobKind.INGEST_TICKER,
        {"symbol": symbol, "retry_exhausted": refresh},
        priority=queue.PRIORITY_INTERACTIVE,
        dedupe_key=queue.ingest_key(symbol),
        source=JobSource.INTERACTIVE,
        user_id=principal.user_id,
        requested_by=principal.key,
    )
    await session.commit()
    if charged and not created:  # another request queued it a moment ago
        await quotas.refund(principal, quota)

    if has_data:
        return _Ensured(
            "refreshing",
            "Showing stored data; a refresh is queued. Follow it at "
            f"GET /jobs/{job.id}.",
            job.id,
            warnings=warnings,
        )
    return _Ensured(
        "ingesting",
        "First-time ingestion queued. Poll this endpoint or GET "
        f"/jobs/{job.id}, or repeat the request with `?wait=true` to block "
        "until it finishes.",
        job.id,
        warnings=warnings,
    )


async def _enrich_what_is_owed(
    session: AsyncSession,
    ticker: Ticker,
    llm: LLMProvider,
    principal: Principal,
    *,
    wait: bool,
) -> _Ensured:
    """Fresh prices, but maybe movements still owed news.

    The nightly run refreshes prices for its whole universe but enriches only
    as many movements as its budget allows; the rest stay PENDING until
    someone asks. This is that someone: their enrichment is enqueued at
    interactive priority. A nightly job already queued for a movement shares
    its dedupe key, so it is pulled to the front rather than duplicated.

    It is real news and LLM work, so a request that starts any of it is
    charged one `cold_ingests_per_day`, however many movements it covers. One
    that only pulls already-queued jobs forward is free.
    """
    owed = await ingestion.enrichable_movements(
        session, ticker, limit=settings.max_movements_per_ingest
    )
    if not owed:
        return _Ensured("ready")
    quota = Quota.COLD_INGESTS_PER_DAY

    if wait:
        async with _inline_slot():
            if (refused := await _charge(principal, quota, has_data=True)) is not None:
                return _Ensured("ready", warnings=[refused])
            warnings: list[str] = []
            for movement in owed:
                try:
                    await ingestion.enrich_movement(session, movement.id, llm=llm)
                except MetrixError as exc:
                    warnings.append(f"{movement.date}: {exc}")
                except WorkDeferred as exc:
                    # Serve what is stored; the remaining movements stay as they were.
                    await session.rollback()
                    await quotas.refund(principal, quota)
                    warnings.append(f"News not fetched: {exc}")
                    break
        return _Ensured("ready", warnings=warnings)

    starts_work = False
    for movement in owed:
        if await queue.active_job(session, queue.enrich_key(movement.id)) is None:
            starts_work = True
            break
    if starts_work and (refused := await _charge(principal, quota, has_data=True)) is not None:
        return _Ensured("ready", warnings=[refused])

    jobs = [
        await queue.enqueue(
            session,
            JobKind.ENRICH_MOVEMENT,
            {"movement_id": movement.id},
            priority=queue.PRIORITY_INTERACTIVE,
            dedupe_key=queue.enrich_key(movement.id),
            source=JobSource.INTERACTIVE,
            user_id=principal.user_id,
            requested_by=principal.key,
        )
        for movement in owed
    ]
    await session.commit()
    return _Ensured(
        "refreshing",
        f"Showing stored data; news for {len(owed)} movement(s) is being fetched. "
        f"Follow the largest at GET /jobs/{jobs[0].id}.",
        jobs[0].id,
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


def _empty_response(
    symbol: str, state: IngestState, message: str | None, job_id: int | None
) -> TickerDetailOut:
    return TickerDetailOut(
        status=state,
        message=message,
        job_id=job_id,
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


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres hands back aware ones."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
