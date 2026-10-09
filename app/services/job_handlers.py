"""What each kind of job does.

A handler receives its own session, the claimed job and a `JobContext`, and
either returns (the job succeeded) or raises (the queue decides between retry
and dead from the exception). Handlers must be safe to run twice: a job whose
worker dies mid-run is reaped and run again, so everything they write is an
upsert or guarded by a status check.

Only the kinds with a handler here are claimed by the worker. Kinds added by
later stages stay queued until a worker that knows them is deployed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from app.core.logging import get_logger
from app.models.jobs import Job, JobKind
from app.models.market import Movement
from app.services import ingestion
from app.services.llm import LLMProvider

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
        await ingestion.ingest_ticker(
            session,
            symbol,
            llm=ctx.llm,
            retry_exhausted=bool(job.payload.get("retry_exhausted")),
            progress=ctx.report_progress,
        )
    except Exception as exc:
        await session.rollback()
        await ingestion.mark_ingestion_failed(session, symbol, exc)
        raise


async def handle_enrich_movement(session: AsyncSession, job: Job, ctx: JobContext) -> None:
    """News for one movement. Needs no ticker claim, since it writes only that
    movement and its links.

    A price refresh may re-run detection and delete the movement while this
    runs. Then the flush fails on a missing row; if the movement is indeed
    gone, there is nothing left to explain and the job succeeds as a no-op.
    """
    movement_id = int(job.payload["movement_id"])
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


HANDLERS: dict[JobKind, Handler] = {
    JobKind.INGEST_TICKER: handle_ingest_ticker,
    JobKind.ENRICH_MOVEMENT: handle_enrich_movement,
}
