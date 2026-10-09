"""The nightly pre-warm run.

Prices are cheap and batchable, so they are refreshed broadly; news
enrichment is the expensive part, so it is spent only on new or unfinished
movements, most-demanded tickers first, within a per-run budget.

    schedule_nightly        select the universe, open a `prewarm_runs` row,
          |                 enqueue one refresh_prices job per chunk
          v
    refresh_prices (xN)     batch-download prices, detect movements, and for
          |                 each movement that needs news enqueue:
          +--> prewarm_sector_macro   the sector's Hard-tier search, once per
          |                           (sector, date), so it is cached first
          +--> enrich_movement        blocked by that macro job; takes a unit
                                      of budget when it starts, or defers

Priority bands (lower runs first)
---------------------------------
    0       interactive: a user is waiting on this
    5       nightly fan-out: schedule_nightly and refresh_prices (cheap, and
            everything else waits on them)
    10      prewarm_sector_macro (one search that unblocks many enrichments)
    20-59   nightly enrichment of tickers people have asked for, by
            popularity, then move size (`enrichment_priority`)
    60-89   nightly enrichment of seed tickers nobody has asked for yet
    90      PARTIAL follow-ups (they wait for a fixed time anyway)

The budget is taken when an enrichment starts, not when it is queued, so it
is spent in priority order: whatever is left when it runs out is the
least-demanded work, deferred to the next night or to the first user who asks.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import PriceDataError, is_permanent
from app.core.logging import get_logger
from app.models.enums import IngestStatus
from app.models.jobs import JobKind, JobSource, PrewarmRun, PrewarmRunStatus
from app.models.market import Movement, PriceBar, Ticker
from app.services import demand, ingestion, queue
from app.services import prices as price_service
from app.services.demand import UniverseEntry
from app.services.ingestion import ProgressCallback

logger = get_logger(__name__)

# ------------------------------------------------------------ priorities

DEMAND_BAND_START = 20
SEED_BAND_START = 60

# Popularity buckets are powers of two: bucket 0 is a score of 2^9 or more
# (about fifty requests a day, every day), bucket 9 is no demand at all. For
# scale, one request a day settles at a score of about 10.6, bucket 6.
POPULARITY_BUCKETS = 10
# Move-size buckets break ties within a popularity bucket: bigger moves first.
MOVE_SIZE_STEPS = (0.10, 0.05, 0.03)  # >=10%, >=5%, >=3%, smaller
MOVE_BUCKETS = len(MOVE_SIZE_STEPS) + 1
# Seed-only tickers spread their move buckets across the 60-89 band.
SEED_MOVE_STRIDE = 7


def enrichment_priority(popularity: float, abs_return: float, *, seeded: bool) -> int:
    """Queue priority for a nightly enrichment. Lower runs, and is paid for, first.

    A ticker with any demand lands in 20-59: four slots per popularity bucket,
    the slot chosen by move size, so popularity dominates and move size only
    breaks ties. A seed ticker nobody has asked for lands in 60-89, ordered by
    move size alone: it is warmed with whatever budget demand left over.
    """
    move = _move_bucket(abs_return)
    if popularity <= 0 and seeded:
        return SEED_BAND_START + SEED_MOVE_STRIDE * move
    return DEMAND_BAND_START + MOVE_BUCKETS * _popularity_bucket(popularity) + move


def _popularity_bucket(popularity: float) -> int:
    doublings = math.floor(math.log2(1 + max(popularity, 0.0)))
    return max(0, POPULARITY_BUCKETS - 1 - doublings)


def _move_bucket(abs_return: float) -> int:
    for bucket, step in enumerate(MOVE_SIZE_STEPS):
        if abs_return >= step:
            return bucket
    return len(MOVE_SIZE_STEPS)


# -------------------------------------------------------------- the run


async def run_for(session: AsyncSession, trading_date: date) -> PrewarmRun | None:
    return await session.scalar(
        sa.select(PrewarmRun).where(PrewarmRun.trading_date == trading_date)
    )


async def start_run(
    session: AsyncSession, trading_date: date, *, now: datetime
) -> PrewarmRun | None:
    """Open the run for `trading_date`, or None if one already exists.

    The unique trading date is what makes scheduling safe from every worker
    replica: whichever gets here first runs the night, the rest find its row.
    """
    if await run_for(session, trading_date) is not None:
        return None
    run = PrewarmRun(
        trading_date=trading_date,
        status=PrewarmRunStatus.RUNNING,
        started_at=now,
        enrichment_budget=settings.prewarm_max_enrichments_per_run,
        universe_size=0,
        movements_found=0,
        enrichments_queued=0,
        enrichments_deferred=0,
        enrichments_used=0,
    )
    try:
        async with session.begin_nested():
            session.add(run)
    except IntegrityError:  # another replica inserted it a moment ago
        return None
    return run


async def schedule_nightly(
    session: AsyncSession, trading_date: date, *, now: datetime
) -> PrewarmRun | None:
    """Start the night: open the run, choose the universe, fan out. Commits.

    Returns None, having done nothing, if this trading date already has a run.
    """
    run = await start_run(session, trading_date, now=now)
    if run is None:
        logger.info("prewarm_run_exists", trading_date=trading_date.isoformat())
        return None

    universe = await demand.select_prewarm_universe(session, now=now)
    size = settings.price_batch_size
    chunks = [universe[i : i + size] for i in range(0, len(universe), size)]
    # The universe is most popular first, so the first chunks matter most.
    # They are not jittered: the token buckets already pace yfinance, and
    # jitter would only shuffle that order.
    for index, chunk in enumerate(chunks):
        await queue.enqueue(
            session,
            JobKind.REFRESH_PRICES,
            {
                "run_id": run.id,
                "trading_date": trading_date.isoformat(),
                "entries": [asdict(entry) for entry in chunk],
            },
            priority=queue.PRIORITY_NIGHTLY_FANOUT,
            dedupe_key=queue.prices_key(trading_date, index),
            source=JobSource.SCHEDULED,
            run_id=run.id,
            now=now,
        )
    run.universe_size = len(universe)
    await session.commit()

    logger.info(
        "prewarm_run_started",
        run_id=run.id,
        trading_date=trading_date.isoformat(),
        universe_size=len(universe),
        chunks=len(chunks),
        enrichment_budget=run.enrichment_budget,
    )
    # An empty universe has no jobs whose completion would close the run.
    await queue.finish_run_if_done(session, run.id, now=now)
    return run


# ------------------------------------------------------- refresh_prices

# A short refresh must overlap the stored bars by at least this many calendar
# days, so a split or dividend since the last refresh shows up on a day both
# have (see `prices.merge_price_bars`).
MIN_OVERLAP_DAYS = 7


def needs_full_history(last_stored: date | None, today: date) -> bool:
    """Whether a ticker needs a full year of prices rather than the short window.

    Full when nothing is stored yet, or when the last stored bar is too old for
    the short window to overlap it by `MIN_OVERLAP_DAYS`.
    """
    if last_stored is None:
        return True
    return (today - last_stored).days > settings.prewarm_price_lookback_days - MIN_OVERLAP_DAYS


@dataclass(slots=True)
class ChunkOutcome:
    refreshed: int = 0
    skipped_claimed: int = 0
    not_found: int = 0
    failed: int = 0
    movements_found: int = 0
    enrichments_queued: int = 0


async def refresh_chunk(
    session: AsyncSession,
    entries: list[UniverseEntry],
    run_id: int,
    *,
    now: datetime,
    progress: ProgressCallback | None = None,
) -> ChunkOutcome:
    """Refresh prices for one chunk of the universe and queue its enrichments.

    Takes each ticker's ingestion claim, as an interactive ingestion does. If
    a user's ingestion holds it, the ticker is skipped: that run is doing the
    same work. One symbol failing never fails the chunk; only a chunk in which
    every attempted symbol failed transiently raises, so the queue retries it.
    Commits per ticker.
    """
    outcome = ChunkOutcome()
    by_symbol = {entry.symbol: entry for entry in entries}
    tickers = {
        symbol: await ingestion.get_or_create_ticker(session, symbol) for symbol in by_symbol
    }
    await session.commit()

    histories = await _fetch_chunk(session, tickers, today=now.date())
    for done, (symbol, ticker) in enumerate(tickers.items(), start=1):
        result = histories[symbol]
        if not await ingestion.claim_ingestion(session, ticker):
            logger.info("refresh_skipped_claim_held", symbol=symbol, run_id=run_id)
            outcome.skipped_claimed += 1
        elif isinstance(result, Exception):
            await ingestion.mark_ingestion_failed(session, symbol, result)
            if is_permanent(result):
                outcome.not_found += 1
            else:
                outcome.failed += 1
            logger.warning("refresh_symbol_failed", symbol=symbol, error=str(result))
        else:
            try:
                await _refresh_one(
                    session, ticker, result, by_symbol[symbol], run_id, now, outcome
                )
            except Exception as exc:
                await session.rollback()
                await ingestion.mark_ingestion_failed(session, symbol, exc)
                outcome.failed += 1
                logger.warning("refresh_symbol_failed", symbol=symbol, error=str(exc))
        if progress is not None:
            await progress({"stage": "refreshing", "done": done, "total": len(tickers)})

    await queue.add_to_run(
        session,
        run_id,
        movements_found=outcome.movements_found,
        enrichments_queued=outcome.enrichments_queued,
    )
    await session.commit()
    logger.info("refresh_chunk_complete", run_id=run_id, **asdict(outcome))

    attempted = len(tickers) - outcome.skipped_claimed - outcome.not_found
    if attempted and outcome.failed == attempted:
        raise PriceDataError("yfinance", f"every symbol in the chunk failed ({attempted})")
    return outcome


async def _fetch_chunk(
    session: AsyncSession, tickers: dict[str, Ticker], *, today: date
) -> dict[str, price_service.PriceHistory | Exception]:
    """Batch-fetch a chunk: the short window where it suffices, a full year
    where it does not, and company profiles only where none is stored."""
    last_bar: dict[int, date] = dict(
        (
            await session.execute(
                sa.select(PriceBar.ticker_id, sa.func.max(PriceBar.date))
                .where(PriceBar.ticker_id.in_([t.id for t in tickers.values()]))
                .group_by(PriceBar.ticker_id)
            )
        ).all()
    )
    full = [s for s, t in tickers.items() if needs_full_history(last_bar.get(t.id), today)]
    short = [s for s in tickers if s not in full]
    want_profile = {s for s, t in tickers.items() if t.company_name is None and t.sector is None}

    histories: dict[str, price_service.PriceHistory | Exception] = {}
    for symbols, days in (
        (short, settings.prewarm_price_lookback_days),
        (full, settings.price_history_days),
    ):
        if symbols:
            histories |= await price_service.fetch_price_histories(
                symbols, days, with_profile=[s for s in symbols if s in want_profile]
            )
    return histories


async def _refresh_one(
    session: AsyncSession,
    ticker: Ticker,
    history: price_service.PriceHistory,
    entry: UniverseEntry,
    run_id: int,
    now: datetime,
    outcome: ChunkOutcome,
) -> None:
    refreshed = await ingestion.refresh_prices(session, ticker, history)
    ticker.ingest_status = IngestStatus.COMPLETE
    ticker.ingest_error = None
    ticker.ingest_error_permanent = False
    ticker.last_ingested_at = now
    outcome.refreshed += 1
    outcome.movements_found += len(refreshed.created)

    for movement in refreshed.unfinished:
        # The rule the on-demand path uses: a PARTIAL movement waits for its
        # window to close, and one that failed NEWS_MAX_ATTEMPTS times is left
        # for an explicit refresh.
        if ingestion.needs_enrichment(movement, now):
            if await enqueue_enrichment(session, ticker, movement, entry, run_id):
                outcome.enrichments_queued += 1
    await session.commit()


async def enqueue_enrichment(
    session: AsyncSession,
    ticker: Ticker,
    movement: Movement,
    entry: UniverseEntry,
    run_id: int,
) -> bool:
    """Queue the sector's macro search, and this movement's enrichment behind it.

    True if a new enrichment job was created; False if one was already active
    (a user asked first, or another chunk got there).
    """
    macro = queue.macro_key(ticker.sector, movement.date)
    await queue.enqueue(
        session,
        JobKind.PREWARM_SECTOR_MACRO,
        {"sector": ticker.sector, "date": movement.date.isoformat()},
        priority=queue.PRIORITY_MACRO,
        dedupe_key=macro,
        source=JobSource.SCHEDULED,
        run_id=run_id,
    )
    _, created = await queue.enqueue_checked(
        session,
        JobKind.ENRICH_MOVEMENT,
        {"movement_id": movement.id, "run_id": run_id},
        priority=enrichment_priority(entry.popularity, movement.abs_return, seeded=entry.seeded),
        dedupe_key=queue.enrich_key(movement.id),
        blocked_by_key=macro,
        source=JobSource.SCHEDULED,
        run_id=run_id,
    )
    return created


def universe_entries(payload: list[dict[str, Any]]) -> list[UniverseEntry]:
    """Rebuild the entries a refresh_prices job was enqueued with."""
    return [UniverseEntry(**item) for item in payload]
