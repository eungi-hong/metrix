"""Tests for the worker and the job handlers, end to end on SQLite.

These use the file-backed `db` fixture (see conftest) rather than the shared
in-memory database: the worker's keepalive writes while a handler's
transaction is open, which needs real, separate connections.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.core.errors import PriceDataError, TickerNotFoundError
from app.models.enums import IngestStatus, NewsStatus
from app.models.jobs import Job, JobKind, JobSource, JobStatus
from app.models.market import Movement, Ticker
from app.services import ingestion, queue
from app.services import prices as price_service
from app.services.news.queries import news_window_closes_at
from app.worker import Worker
from tests.test_freshness import history_ending_on


@pytest.fixture
def worker(db, stub_llm) -> Worker:
    return Worker(session_factory=db, llm=stub_llm, concurrency=2, interactive_slots=1)


async def enqueue(db, kind: JobKind, payload: dict, key: str, *, priority: int = 0, **kwargs) -> Job:
    async with db() as session:
        job = await queue.enqueue(
            session, kind, payload, priority=priority, dedupe_key=key,
            source=JobSource.INTERACTIVE, **kwargs,
        )
        await session.commit()
        return job


async def ingest_job(db, symbol: str, **kwargs) -> Job:
    return await enqueue(
        db, JobKind.INGEST_TICKER, {"symbol": symbol}, queue.ingest_key(symbol), **kwargs
    )


async def load(db, model, ident):
    async with db() as session:
        return await session.get(model, ident)


async def jobs(db) -> list[Job]:
    async with db() as session:
        return list((await session.scalars(sa.select(Job).order_by(Job.id))).all())


# ------------------------------------------------------------ ingest_ticker


async def test_an_ingest_job_runs_the_pipeline(db, worker):
    queued = await ingest_job(db, "COLD")

    ran = await worker.run_once()

    assert ran.id == queued.id
    job = await load(db, Job, queued.id)
    assert job.status == JobStatus.SUCCEEDED
    assert job.finished_at is not None
    assert job.progress == {"stage": "enriching", "done": 1, "total": 1}

    async with db() as session:
        ticker = await ingestion.get_ticker(session, "COLD")
        assert ticker.ingest_status == IngestStatus.COMPLETE
        movement = (await session.scalars(sa.select(Movement))).one()
        assert movement.news_status == NewsStatus.COMPLETE


async def test_an_ingest_job_skips_a_ticker_someone_else_is_ingesting(db, worker, monkeypatch):
    """A `wait=true` request holds the claim; it is already doing this work."""
    async with db() as session:
        ticker = await ingestion.get_or_create_ticker(session, "BUSY")
        assert await ingestion.claim_ingestion(session, ticker)

    async def explode(*args, **kwargs):
        raise AssertionError("must not fetch while another run holds the claim")

    monkeypatch.setattr(price_service, "fetch_price_history", explode)
    queued = await ingest_job(db, "BUSY")

    await worker.run_once()

    assert (await load(db, Job, queued.id)).status == JobStatus.SUCCEEDED


async def test_an_unknown_ticker_is_dead_at_once_and_reported_on_the_ticker(db, worker, monkeypatch):
    async def not_found(symbol: str, days=None):
        raise TickerNotFoundError(symbol)

    monkeypatch.setattr(price_service, "fetch_price_history", not_found)
    queued = await ingest_job(db, "NOPE")

    await worker.run_once()

    job = await load(db, Job, queued.id)
    assert job.status == JobStatus.DEAD
    assert job.attempts == 1
    assert "TickerNotFoundError" in job.last_error
    async with db() as session:
        ticker = await ingestion.get_ticker(session, "NOPE")
        assert ticker.ingest_status == IngestStatus.FAILED
        assert ticker.ingest_error_permanent, "so pre-warming stops trying it"


async def test_a_transient_failure_is_requeued_with_backoff(db, worker, monkeypatch):
    async def flaky(symbol: str, days=None):
        raise PriceDataError("yfinance", "connection reset")

    monkeypatch.setattr(price_service, "fetch_price_history", flaky)
    queued = await ingest_job(db, "FLAKY")

    await worker.run_once()

    job = await load(db, Job, queued.id)
    assert job.status == JobStatus.QUEUED
    assert job.run_after.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    assert await worker.run_once() is None, "not claimable until the backoff passes"
    async with db() as session:
        ticker = await ingestion.get_ticker(session, "FLAKY")
        assert ticker.ingest_status == IngestStatus.FAILED, "the claim is released"
        assert not ticker.ingest_error_permanent


async def test_a_successful_ingestion_clears_a_permanent_failure(db, worker):
    async with db() as session:
        session.add(
            Ticker(symbol="BACK", ingest_status=IngestStatus.FAILED, ingest_error_permanent=True)
        )
        await session.commit()
    await ingest_job(db, "BACK")

    await worker.run_once()

    async with db() as session:
        ticker = await ingestion.get_ticker(session, "BACK")
        assert ticker.ingest_status == IngestStatus.COMPLETE
        assert not ticker.ingest_error_permanent


# ---------------------------------------------------------- enrich_movement


async def test_enrich_for_a_deleted_movement_succeeds_as_a_no_op(db, worker, stub_llm):
    queued = await enqueue(
        db, JobKind.ENRICH_MOVEMENT, {"movement_id": 4242}, queue.enrich_key(4242)
    )

    await worker.run_once()

    assert (await load(db, Job, queued.id)).status == JobStatus.SUCCEEDED
    assert stub_llm.structured_calls == []


async def test_a_partial_enrichment_enqueues_its_followup_at_window_close(
    db, worker, monkeypatch
):
    """Run as an `enrich:{id}` job, the PARTIAL pass must still produce its
    follow-up: the dedupe must not mistake it for the running job."""
    today = datetime.now(timezone.utc).date()

    async def fetch(symbol: str, days=None):
        return history_ending_on(today, symbol)

    monkeypatch.setattr(price_service, "fetch_price_history", fetch)

    # Prices and movements only, so the movement is still PENDING.
    async with db() as session:
        ticker = await ingestion.get_or_create_ticker(session, "NEW")
        history = await price_service.fetch_price_history("NEW")
        (movement,) = (await ingestion.refresh_prices(session, ticker, history)).created
        await session.commit()

    queued = await enqueue(
        db, JobKind.ENRICH_MOVEMENT, {"movement_id": movement.id},
        queue.enrich_key(movement.id), priority=30,
    )
    await worker.run_once()

    assert (await load(db, Job, queued.id)).status == JobStatus.SUCCEEDED
    assert (await load(db, Movement, movement.id)).news_status == NewsStatus.PARTIAL

    followup = next(j for j in await jobs(db) if j.id != queued.id)
    assert followup.dedupe_key == f"enrich:{movement.id}:followup"
    assert followup.status == JobStatus.QUEUED
    assert followup.kind == JobKind.ENRICH_MOVEMENT
    assert followup.source == JobSource.FOLLOWUP
    assert followup.priority == queue.PRIORITY_FOLLOWUP
    assert followup.run_after.replace(tzinfo=timezone.utc) == news_window_closes_at(today)
    assert await worker.run_once() is None, "the follow-up waits for the window to close"


# ------------------------------------------------------------------ lanes


async def test_the_interactive_lane_leaves_background_work_alone(db, worker):
    await ingest_job(db, "LATER", priority=20)

    assert await worker.run_once(interactive=True) is None
    assert (await worker.run_once()).dedupe_key == "ingest:LATER"


async def test_kinds_without_a_handler_are_not_claimed(db, stub_llm):
    """A worker from an older deploy leaves newer kinds for one that knows them."""
    from app.services.job_handlers import HANDLERS

    old_worker = Worker(
        session_factory=db, llm=stub_llm,
        handlers={JobKind.INGEST_TICKER: HANDLERS[JobKind.INGEST_TICKER]},
    )
    await enqueue(db, JobKind.REFRESH_PRICES, {"entries": []}, "prices:chunk-0")

    assert await old_worker.run_once() is None
    assert (await jobs(db))[0].status == JobStatus.QUEUED


# --------------------------------------------------------------- shutdown


async def test_shutdown_lets_a_quick_job_finish(db, stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "worker_poll_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "worker_poll_jitter_seconds", 0)
    started, finish = asyncio.Event(), asyncio.Event()

    async def handler(session, job, ctx):
        started.set()
        await finish.wait()

    worker = Worker(
        session_factory=db, llm=stub_llm, concurrency=1, interactive_slots=0,
        handlers={JobKind.INGEST_TICKER: handler},
    )
    queued = await ingest_job(db, "QUICK")
    running = asyncio.create_task(worker.run())
    await asyncio.wait_for(started.wait(), timeout=5)

    worker.stop()
    finish.set()
    await asyncio.wait_for(running, timeout=5)

    assert (await load(db, Job, queued.id)).status == JobStatus.SUCCEEDED


async def test_shutdown_releases_a_job_that_overruns_the_timeout(db, stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "worker_poll_interval_seconds", 0.01)
    monkeypatch.setattr(settings, "worker_poll_jitter_seconds", 0)
    monkeypatch.setattr(settings, "worker_shutdown_timeout_seconds", 0.05)
    started = asyncio.Event()

    async def stuck(session, job, ctx):
        started.set()
        await asyncio.sleep(3600)

    worker = Worker(
        session_factory=db, llm=stub_llm, concurrency=1, interactive_slots=0,
        handlers={JobKind.INGEST_TICKER: stuck},
    )
    queued = await ingest_job(db, "SLOW")
    running = asyncio.create_task(worker.run())
    await asyncio.wait_for(started.wait(), timeout=5)

    worker.stop()
    await asyncio.wait_for(running, timeout=5)

    job = await load(db, Job, queued.id)
    assert job.status == JobStatus.QUEUED
    assert job.attempts == 0, "a released job gets its attempt back"
    assert job.locked_by is None


async def test_progress_is_published_while_the_job_runs(db, stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "worker_poll_interval_seconds", 0.01)
    reported, release = asyncio.Event(), asyncio.Event()

    async def handler(session, job, ctx):
        await ctx.report_progress({"done": 1, "total": 3})
        reported.set()
        await release.wait()

    worker = Worker(
        session_factory=db, llm=stub_llm, concurrency=1, interactive_slots=0,
        handlers={JobKind.INGEST_TICKER: handler},
    )
    queued = await ingest_job(db, "PROG")
    running = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(reported.wait(), timeout=5)

    for _ in range(100):  # the keepalive publishes it shortly after
        if (await load(db, Job, queued.id)).progress:
            break
        await asyncio.sleep(0.01)
    assert (await load(db, Job, queued.id)).progress == {"done": 1, "total": 3}
    assert (await load(db, Job, queued.id)).status == JobStatus.RUNNING

    release.set()
    await asyncio.wait_for(running, timeout=5)
