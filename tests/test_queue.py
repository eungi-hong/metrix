"""Tests for the job queue's semantics: dedupe, ordering, blocking, retries,
locks, and the per-run enrichment budget."""

from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.core.errors import (
    ConfigurationError,
    LLMError,
    NewsProviderError,
    PriceDataError,
    TickerNotFoundError,
)
from app.models.jobs import Job, JobKind, JobSource, JobStatus, PrewarmRun, PrewarmRunStatus
from app.services import queue
from tests.conftest import POSTGRES, postgres_session_factory, sqlite_session_factory

T0 = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=POSTGRES)])
async def session_factory(request):
    """Every queue test runs on SQLite, and on Postgres when it is available:
    the claim is a different statement on each, so both must be held to the
    same semantics."""
    factory = sqlite_session_factory if request.param == "sqlite" else postgres_session_factory
    async with factory() as made:
        yield made


async def add(session, key: str, *, priority: int = 50, run_after=None, kind=JobKind.ENRICH_MOVEMENT, **kwargs) -> Job:
    job = await queue.enqueue(
        session,
        kind,
        {"key": key},
        priority=priority,
        dedupe_key=key,
        source=JobSource.SCHEDULED,
        run_after=run_after or T0,
        now=T0,
        **kwargs,
    )
    await session.commit()
    return job


async def claim(session, worker: str = "w1", *, now=T0, **kwargs) -> Job | None:
    return await queue.claim(session, worker, now=now, **kwargs)


async def reload(session, job: Job) -> Job:
    fresh = await session.get(Job, job.id, populate_existing=True)
    assert fresh is not None
    return fresh


def utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- enqueue


async def test_a_duplicate_request_returns_the_queued_job(session):
    first = await add(session, "enrich:1")
    second = await add(session, "enrich:1")

    assert second.id == first.id
    assert await session.scalar(sa.select(sa.func.count()).select_from(Job)) == 1


async def test_a_duplicate_raises_priority_and_pulls_run_after_forward(session):
    queued = await add(session, "ingest:NVDA", priority=40, run_after=T0 + timedelta(hours=5))

    bumped = await add(session, "ingest:NVDA", priority=0, run_after=T0)

    assert bumped.id == queued.id
    assert bumped.priority == 0
    assert utc(bumped.run_after) == T0


async def test_a_duplicate_never_lowers_priority_or_delays_the_job(session):
    await add(session, "ingest:NVDA", priority=0, run_after=T0)

    job = await add(session, "ingest:NVDA", priority=60, run_after=T0 + timedelta(hours=5))

    assert job.priority == 0
    assert utc(job.run_after) == T0


async def test_a_duplicate_of_a_running_job_returns_it_unchanged(session):
    running = await add(session, "ingest:NVDA", priority=40)
    await claim(session)

    again = await add(session, "ingest:NVDA", priority=0)

    assert again.id == running.id
    assert again.status == JobStatus.RUNNING
    assert again.priority == 40


async def test_a_finished_job_does_not_block_new_work_under_its_key(session):
    first = await add(session, "enrich:1")
    await queue.complete(session, await claim(session), now=T0)

    second = await add(session, "enrich:1")

    assert second.id != first.id
    assert second.status == JobStatus.QUEUED


async def test_an_enqueue_rolls_back_with_the_transaction_that_caused_it(session):
    """The reason for a Postgres queue: a job commits with its cause, or not at all."""
    await queue.enqueue(
        session, JobKind.ENRICH_MOVEMENT, {}, priority=1, dedupe_key="enrich:9",
        source=JobSource.FOLLOWUP,
    )
    await session.rollback()

    assert await session.scalar(sa.select(sa.func.count()).select_from(Job)) == 0


def test_keys_are_normalised():
    assert queue.ingest_key(" nvda ") == "ingest:NVDA"
    assert queue.macro_key(None, date(2026, 7, 2)) == "macro:_unknown:2026-07-02"
    assert queue.macro_key("  ", date(2026, 7, 2)) == "macro:_unknown:2026-07-02"
    assert queue.macro_key("Technology", date(2026, 7, 2)) == "macro:Technology:2026-07-02"


async def test_a_followup_is_not_swallowed_by_its_running_enrich_job(session):
    """The follow-up is enqueued from inside the running `enrich:{id}` job.
    Under the same key, dedupe would return the running job and drop it."""
    await add(session, queue.enrich_key(7))
    assert (await claim(session)).dedupe_key == "enrich:7"

    followup = await add(session, queue.followup_key(7), priority=queue.PRIORITY_FOLLOWUP)

    assert followup.status == JobStatus.QUEUED
    assert followup.dedupe_key == "enrich:7:followup"


# ------------------------------------------------------------------ claim


async def test_claims_go_by_priority_then_run_after_then_age(session):
    await add(session, "c", priority=20, run_after=T0 - timedelta(minutes=1))
    await add(session, "a", priority=10, run_after=T0 - timedelta(minutes=1))
    await add(session, "b", priority=20, run_after=T0 - timedelta(minutes=5))
    await add(session, "d", priority=20, run_after=T0 - timedelta(minutes=1))

    order = [(await claim(session)).dedupe_key for _ in range(4)]

    assert order == ["a", "b", "c", "d"]
    assert await claim(session) is None


async def test_a_job_is_not_claimable_before_run_after(session):
    await add(session, "later", run_after=T0 + timedelta(hours=1))

    assert await claim(session, now=T0) is None
    assert (await claim(session, now=T0 + timedelta(hours=1))).dedupe_key == "later"


async def test_two_claims_never_return_the_same_job(session):
    for i in range(5):
        await add(session, f"job:{i}")

    claimed = [await claim(session, f"w{i}") for i in range(6)]
    ids = [job.id for job in claimed if job is not None]

    assert len(ids) == 5 == len(set(ids))
    assert claimed[-1] is None


async def test_claim_marks_the_job_running_and_counts_the_attempt(session):
    await add(session, "x")

    job = await claim(session, "worker-a")

    assert job.status == JobStatus.RUNNING
    assert job.locked_by == "worker-a"
    assert utc(job.locked_at) == T0
    assert job.attempts == 1


async def test_claim_can_be_limited_to_kinds(session):
    await add(session, "prices", kind=JobKind.REFRESH_PRICES, priority=0)
    await add(session, "enrich", kind=JobKind.ENRICH_MOVEMENT, priority=50)

    job = await claim(session, kinds=[JobKind.ENRICH_MOVEMENT])

    assert job.dedupe_key == "enrich"


async def test_the_interactive_lane_never_claims_background_work(session):
    await add(session, "nightly", priority=20)
    assert await claim(session, max_priority=queue.PRIORITY_INTERACTIVE) is None

    await add(session, "user", priority=queue.PRIORITY_INTERACTIVE)
    assert (await claim(session, max_priority=0)).dedupe_key == "user"


# --------------------------------------------------------------- blocking


async def test_a_blocked_job_waits_for_its_blocker(session):
    await add(session, "macro:Tech:2026-07-02", priority=10)
    await add(session, "enrich:1", priority=5, blocked_by_key="macro:Tech:2026-07-02")

    # The dependent has the better priority but cannot run yet.
    blocker = await claim(session)
    assert blocker.dedupe_key == "macro:Tech:2026-07-02"
    assert await claim(session) is None, "still blocked while the blocker runs"

    await queue.complete(session, blocker, now=T0)
    assert (await claim(session)).dedupe_key == "enrich:1"


async def test_a_dead_blocker_releases_its_dependents(session):
    """An enrich job whose macro job died runs its own macro search live; it
    must not hang forever."""
    await add(session, "macro:Tech:2026-07-02", priority=10, max_attempts=1)
    await add(session, "enrich:1", blocked_by_key="macro:Tech:2026-07-02")

    blocker = await claim(session)
    assert await queue.fail(session, blocker, PriceDataError("x", "boom"), now=T0) == JobStatus.DEAD

    assert (await claim(session)).dedupe_key == "enrich:1"


async def test_a_blocker_key_with_no_active_job_does_not_block(session):
    await add(session, "enrich:1", blocked_by_key="macro:Tech:never-enqueued")
    assert (await claim(session)).dedupe_key == "enrich:1"


# ---------------------------------------------------------------- failure


async def test_a_transient_failure_is_retried_later(session, monkeypatch):
    monkeypatch.setattr(settings, "job_retry_base_seconds", 60)
    await add(session, "x")
    job = await claim(session)

    status = await queue.fail(session, job, NewsProviderError("exa", "HTTP 503"), now=T0)

    job = await reload(session, job)
    assert status == JobStatus.QUEUED
    assert job.status == JobStatus.QUEUED
    assert job.locked_by is None
    assert "HTTP 503" in job.last_error
    delay = utc(job.run_after) - T0
    assert timedelta(seconds=45) <= delay <= timedelta(seconds=75)
    assert await claim(session, now=T0) is None, "backoff must be respected"
    assert await claim(session, now=T0 + delay) is not None


@pytest.mark.parametrize(
    "error",
    [
        TickerNotFoundError("NOPE"),
        ConfigurationError("EXA_API_KEY is not set"),
        NewsProviderError("exa", "rejected the API key (HTTP 401)", permanent=True),
        LLMError("anthropic", "authentication failed", permanent=True),
    ],
)
async def test_a_permanent_error_goes_straight_to_dead(session, error):
    await add(session, "x", max_attempts=5)
    job = await claim(session)

    assert await queue.fail(session, job, error, now=T0) == JobStatus.DEAD

    job = await reload(session, job)
    assert job.status == JobStatus.DEAD
    assert job.attempts == 1
    assert job.finished_at is not None


async def test_a_job_that_keeps_failing_ends_dead_after_max_attempts(session):
    await add(session, "x", max_attempts=3)
    later = T0
    statuses = []
    for _ in range(3):
        later += timedelta(days=1)  # past any backoff
        job = await claim(session, now=later)
        statuses.append(await queue.fail(session, job, LLMError("anthropic", "rate limited"), now=later))

    assert statuses == [JobStatus.QUEUED, JobStatus.QUEUED, JobStatus.DEAD]
    assert (await reload(session, job)).attempts == 3


def test_backoff_doubles_is_capped_and_jittered(monkeypatch):
    monkeypatch.setattr(settings, "job_retry_base_seconds", 10)
    monkeypatch.setattr(settings, "job_retry_max_seconds", 100)
    rng = random.Random(0)
    jitter = queue.RETRY_JITTER_FRACTION

    for attempts, nominal in [(1, 10), (2, 20), (3, 40), (4, 80), (5, 100), (12, 100)]:
        seconds = queue.retry_delay(attempts, rng).total_seconds()
        assert nominal * (1 - jitter) <= seconds <= nominal * (1 + jitter)

    spread = {queue.retry_delay(3, rng).total_seconds() for _ in range(20)}
    assert len(spread) > 1, "jitter must actually vary the delay"


# ------------------------------------------------------------------ locks


async def test_a_job_whose_worker_died_is_reaped(session, monkeypatch):
    monkeypatch.setattr(settings, "job_lock_timeout_minutes", 10)
    await add(session, "x")
    job = await claim(session, now=T0)

    assert await queue.reap_stale(session, now=T0 + timedelta(minutes=5)) == 0
    assert await queue.reap_stale(session, now=T0 + timedelta(minutes=11)) == 1

    job = await reload(session, job)
    assert job.status == JobStatus.QUEUED
    assert job.locked_by is None
    assert job.attempts == 1, "the orphaned attempt still counts"


async def test_a_heartbeating_job_is_not_reaped(session, monkeypatch):
    monkeypatch.setattr(settings, "job_lock_timeout_minutes", 10)
    await add(session, "slow")
    job = await claim(session, now=T0)

    for minute in (8, 16, 24):
        assert await queue.heartbeat(session, job, now=T0 + timedelta(minutes=minute))
        assert await queue.reap_stale(session, now=T0 + timedelta(minutes=minute + 9)) == 0

    assert (await reload(session, job)).status == JobStatus.RUNNING


async def test_a_reaped_job_at_max_attempts_is_dead_not_requeued(session, monkeypatch):
    monkeypatch.setattr(settings, "job_lock_timeout_minutes", 10)
    await add(session, "poison", max_attempts=1)
    job = await claim(session, now=T0)

    await queue.reap_stale(session, now=T0 + timedelta(minutes=11))

    assert (await reload(session, job)).status == JobStatus.DEAD


async def test_a_worker_that_lost_its_lock_cannot_overwrite_the_new_owner(session, monkeypatch):
    monkeypatch.setattr(settings, "job_lock_timeout_minutes", 10)
    await add(session, "x")
    original = await claim(session, "slow-worker", now=T0)
    session.expunge(original)  # the slow worker's own copy, as in its own process
    later = T0 + timedelta(minutes=11)
    await queue.reap_stale(session, now=later)
    current = await claim(session, "new-worker", now=later)

    assert await queue.complete(session, original, now=later) is False
    assert await queue.heartbeat(session, original, now=later) is False

    job = await reload(session, current)
    assert job.status == JobStatus.RUNNING
    assert job.locked_by == "new-worker"


async def test_release_requeues_and_refunds_the_attempt(session):
    await add(session, "x")
    job = await claim(session)

    assert await queue.release(session, job, now=T0)

    job = await reload(session, job)
    assert job.status == JobStatus.QUEUED
    assert job.attempts == 0
    assert job.locked_by is None


async def test_progress_is_recorded_on_the_job(session):
    await add(session, "x")
    job = await claim(session)

    await queue.heartbeat(session, job, progress={"stage": "enriching", "done": 4, "total": 10})

    assert (await reload(session, job)).progress == {"stage": "enriching", "done": 4, "total": 10}


# ----------------------------------------------------------------- budget


async def new_run(session, budget: int) -> PrewarmRun:
    run = PrewarmRun(trading_date=date(2026, 10, 9), enrichment_budget=budget)
    session.add(run)
    await session.commit()
    return run


async def test_the_budget_is_never_exceeded_by_two_spenders(session_factory):
    """Two price chunks spend one run's budget, alternating transactions.

    The in-memory SQLite database is a single connection, so the two spenders
    take turns rather than truly overlapping; the overlapping version runs
    against Postgres in test_postgres.py.
    """
    async with session_factory() as session:
        run = await new_run(session, budget=7)

    turn = asyncio.Lock()

    async def chunk(attempts: int) -> list[bool]:
        taken = []
        async with session_factory() as session:
            for _ in range(attempts):
                async with turn:
                    taken.append(await queue.take_enrichment_budget(session, run.id))
                await asyncio.sleep(0)  # let the other chunk go next
        return taken

    first, second = await asyncio.gather(chunk(6), chunk(6))

    assert sum(first) + sum(second) == 7
    assert sum(first) > 0 and sum(second) > 0
    async with session_factory() as session:
        stored = await session.get(PrewarmRun, run.id)
        assert stored.enrichments_used == 7


async def test_a_run_finishes_when_its_last_job_does(session):
    run = await new_run(session, budget=10)
    await add(session, "a", run_id=run.id)
    await add(session, "b", run_id=run.id, max_attempts=1)

    await queue.complete(session, await claim(session), now=T0)
    assert (await session.get(PrewarmRun, run.id, populate_existing=True)).status == PrewarmRunStatus.RUNNING

    await queue.fail(session, await claim(session), TickerNotFoundError("B"), now=T0)
    run = await session.get(PrewarmRun, run.id, populate_existing=True)
    assert run.status == PrewarmRunStatus.FINISHED
    assert run.finished_at is not None
