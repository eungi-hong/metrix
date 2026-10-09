"""What each kind of job does.

A handler receives its own session, the claimed job and a `JobContext`, and
either returns (the job succeeded) or raises (the queue decides between retry
and dead from the exception). Handlers must be safe to run twice: a job whose
worker dies mid-run is reaped and run again, so everything they write is an
upsert or guarded by a status check.

Only the kinds with a handler here are claimed by the worker, so a kind added
later stays queued until a worker that knows it is deployed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.core.errors import WorkDeferred
from app.core.logging import get_logger
from app.models.jobs import Job, JobKind
from app.models.market import Movement
from app.services import ingestion, prewarm, queue, symbol_directory
from app.services.llm import LLMProvider
from app.services.news import build_news_provider
from app.services.news.queries import hard_tier_request

logger = get_logger(__name__)


@dataclass(slots=True)
class JobContext:
    """What a handler needs besides its session."""

    job: Job
    llm: LLMProvider
    progress: dict[str, Any] | None = None
    progress_changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def report_progress(self, progress: dict[str, Any]) -> None:
        """Record progress for the worker to publish on the job.

        Never touches the database itself. The worker's keepalive task writes
        the latest value alongside the lock heartbeat, so a handler is never
        slowed, blocked or failed by progress reporting, and a burst of
        updates costs one write rather than one each.
        """
        self.progress = dict(progress)
        self.progress_changed.set()


Handler = Callable[[AsyncSession, Job, JobContext], Awaitable[None]]


async def handle_ingest_ticker(session: AsyncSession, job: Job, ctx: JobContext) -> None:
    """The on-demand pipeline for one ticker.

    Takes the ticker claim, the single guard against two runs writing one
    ticker's bars and movements at once. If someone else holds it (a
    `wait=true` request is ingesting inline), that run is doing this job's
    work already, so this one succeeds without repeating it.
    """
    symbol = job.payload["symbol"]
    ticker = await ingestion.get_or_create_ticker(session, symbol)
    if not await ingestion.claim_ingestion(session, ticker):
        logger.info("ingest_job_skipped_claim_held", job_id=job.id, symbol=symbol)
        return

    try:
        result = await ingestion.ingest_ticker(
            session,
            symbol,
            llm=ctx.llm,
            retry_exhausted=bool(job.payload.get("retry_exhausted")),
            progress=ctx.report_progress,
        )
    except WorkDeferred:
        raise  # not a failure; ingest_ticker has released the claim
    except Exception as exc:
        await session.rollback()
        await ingestion.mark_ingestion_failed(session, symbol, exc)
        raise
    if result.deferred is not None:
        # The prices are in; the rest of the news waits, held or retried.
        raise result.deferred


async def handle_enrich_movement(session: AsyncSession, job: Job, ctx: JobContext) -> None:
    """News for one movement. Needs no ticker claim, since it writes only that
    movement and its links.

    A price refresh may re-run detection and delete the movement while this
    runs. Then the flush fails on a missing row; if the movement is indeed
    gone, there is nothing left to explain and the job succeeds as a no-op.
    """
    movement_id = int(job.payload["movement_id"])
    if await _deferred_by_budget(session, job, ctx, movement_id):
        return
    try:
        await ingestion.enrich_movement(
            session,
            movement_id,
            llm=ctx.llm,
            retry_exhausted=bool(job.payload.get("retry_exhausted")),
        )
    except (IntegrityError, StaleDataError):
        await session.rollback()
        if await session.get(Movement, movement_id) is None:
            logger.info("enrich_movement_deleted_mid_run", job_id=job.id, movement_id=movement_id)
            return
        raise


async def _deferred_by_budget(
    session: AsyncSession, job: Job, ctx: JobContext, movement_id: int
) -> bool:
    """Take a unit of the nightly run's budget, or defer. True if deferred.

    Only nightly enrichments pay: interactive work and PARTIAL follow-ups
    carry no `run_id`. A nightly job a user's request bumped to priority 0 is
    interactive now, and does not pay either. A movement that is gone or
    already done costs nothing, so that is checked before paying.

    A deferred job succeeds with `progress = {"outcome": "deferred"}`; its
    movement stays PENDING for the next night or the first user who asks.
    """
    run_id = job.payload.get("run_id")
    if run_id is None or job.priority <= queue.PRIORITY_INTERACTIVE:
        return False
    movement = await session.get(Movement, movement_id)
    if movement is None or not ingestion.needs_enrichment(movement, ingestion._now()):
        return False  # enrich_movement will log and skip it
    if await queue.take_enrichment_budget(session, run_id, job=job):
        return False

    await queue.add_to_run(session, run_id, enrichments_deferred=1)
    await session.commit()
    await ctx.report_progress({"outcome": "deferred"})
    logger.info("enrichment_deferred", job_id=job.id, movement_id=movement_id, run_id=run_id)
    return True


async def handle_schedule_nightly(session: AsyncSession, job: Job, ctx: JobContext) -> None:
    """Open the night's run and fan out its price refreshes. Once per date."""
    trading_date = date.fromisoformat(job.payload["trading_date"])
    run = await prewarm.schedule_nightly(session, trading_date, now=ingestion._now())
    if run is not None:
        await ctx.report_progress({"run_id": run.id, "universe_size": run.universe_size})


async def handle_refresh_prices(session: AsyncSession, job: Job, ctx: JobContext) -> None:
    """One chunk of the universe: prices, detection, then queue its enrichments."""
    outcome = await prewarm.refresh_chunk(
        session,
        prewarm.universe_entries(job.payload["entries"]),
        int(job.payload["run_id"]),
        now=ingestion._now(),
        progress=ctx.report_progress,
    )
    await ctx.report_progress({"stage": "done", **asdict(outcome)})


async def handle_prewarm_sector_macro(
    session: AsyncSession, job: Job, ctx: JobContext
) -> None:
    """Run one sector's Hard-tier search for one date, through the cache.

    Its only product is the `news_query_cache` row: every enrichment for that
    sector and date then finds its macro search already answered.
    """
    sector = job.payload.get("sector")
    movement_date = date.fromisoformat(job.payload["date"])
    provider = build_news_provider(session)
    try:
        found = await provider.search(hard_tier_request(sector, movement_date))
    finally:
        await provider.aclose()
    await session.commit()
    logger.info(
        "sector_macro_prewarmed",
        sector=sector,
        date=movement_date.isoformat(),
        articles=len(found),
    )


async def handle_refresh_symbol_directory(
    session: AsyncSession, job: Job, ctx: JobContext
) -> None:
    """Replace the symbol directory with this week's files, or keep the old one.

    A failed download or an implausible result raises, so the queue retries
    with backoff; the table is untouched until a refresh succeeds.
    """
    outcome = await symbol_directory.refresh(session)
    await ctx.report_progress({"before": outcome.before, "after": outcome.after})


HANDLERS: dict[JobKind, Handler] = {
    JobKind.INGEST_TICKER: handle_ingest_ticker,
    JobKind.ENRICH_MOVEMENT: handle_enrich_movement,
    JobKind.SCHEDULE_NIGHTLY: handle_schedule_nightly,
    JobKind.REFRESH_PRICES: handle_refresh_prices,
    JobKind.PREWARM_SECTOR_MACRO: handle_prewarm_sector_macro,
    JobKind.REFRESH_SYMBOL_DIRECTORY: handle_refresh_symbol_directory,
}
