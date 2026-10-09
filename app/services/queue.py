"""A durable, prioritised job queue on Postgres.

Why hand-rolled
---------------
The whole queue is a table, one index for claiming, one partial unique index
for deduplication, and the functions below. A library would hide exactly the
parts worth understanding, and Postgres already provides the hard primitive:
`FOR UPDATE SKIP LOCKED`, which lets many workers pull from one table without
blocking on each other or taking the same row twice.

Semantics
---------
* Lower `priority` runs first; ties go to the earliest `run_after`, then the
  oldest job.
* `dedupe_key` names the work. While a job with a given key is active
  (queued or running), enqueueing the same key returns that job instead of
  adding another. A queued job is pulled forward instead: its priority becomes
  the lower of the two and its `run_after` the earlier, which is how a user
  asking for a ticker already queued for tonight makes it run now. A running
  job is returned unchanged; it is already doing the work.
* `blocked_by_key` holds a job back while an active job holds that key. Any
  terminal state of the blocker releases it, including DEAD: a dependent must
  be able to run without its dependency's head start, never hang on it.
* A failed attempt is retried with exponential backoff and jitter until
  `max_attempts`, then the job is DEAD. Errors flagged `permanent` (an unknown
  symbol, a missing or rejected key) skip the retries and go straight to DEAD.
* A worker that dies leaves its jobs RUNNING. `reap_stale` requeues any whose
  lock has not been refreshed within `JOB_LOCK_TIMEOUT_MINUTES`; a live
  worker refreshes its locks with `heartbeat`, so a slow job is not run twice.
* Every transition sends `NOTIFY job_events, '<job id>'` on Postgres, in the
  same transaction. Nothing listens yet; it is there for a live frontend.

Every transition after the claim is a conditional UPDATE that also matches
`locked_by`. If a job was reaped and claimed by another worker while the first
was still running it, the first worker's late `complete` or `fail` matches
nothing and is dropped, rather than overwriting the new owner's state.

Time is taken from the application clock, not `now()` in the database, so
tests can move it; `now` is a parameter wherever it matters.
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.core.errors import is_permanent
from app.core.logging import get_logger
from app.models.jobs import (
    ACTIVE_JOB_STATUSES,
    Job,
    JobKind,
    JobSource,
    JobStatus,
    PrewarmRun,
    PrewarmRunStatus,
)

logger = get_logger(__name__)

# Priority bands, lower runs first; `app.services.prewarm` documents the full
# table. Interactive work is a band of its own so the worker's interactive
# lane can claim exactly it (`max_priority=0`).
PRIORITY_INTERACTIVE = 0
PRIORITY_NIGHTLY_FANOUT = 5
PRIORITY_MACRO = 10
PRIORITY_FOLLOWUP = 90

# Retry delays are scaled by a random factor in [1 - f, 1 + f], so jobs that
# failed together (a provider outage) do not all retry in the same second.
RETRY_JITTER_FRACTION = 0.25

# The SQLite claim is select-then-conditional-update; losing the race to
# another claimer just means trying the next candidate.
_SQLITE_CLAIM_ATTEMPTS = 5

NOTIFY_CHANNEL = "job_events"
UNKNOWN_SECTOR = "_unknown"


# ------------------------------------------------------------------- keys


def ingest_key(symbol: str) -> str:
    return f"ingest:{symbol.strip().upper()}"


def enrich_key(movement_id: int) -> str:
    return f"enrich:{movement_id}"


def followup_key(movement_id: int) -> str:
    """Separate from `enrich_key` on purpose.

    A PARTIAL enrichment enqueues its follow-up from inside the running
    `enrich:{id}` job. Under the same key, the dedupe would find that running
    job and return it, and the follow-up would silently never exist.
    """
    return f"enrich:{movement_id}:followup"


def macro_key(sector: str | None, day: date) -> str:
    return f"macro:{(sector or '').strip() or UNKNOWN_SECTOR}:{day.isoformat()}"


def nightly_key(trading_date: date) -> str:
    return f"nightly:{trading_date.isoformat()}"


def prices_key(trading_date: date, chunk: int) -> str:
    return f"prices:{trading_date.isoformat()}:{chunk}"


def jittered(at: datetime, *, not_before: datetime | None = None) -> datetime:
    """`at` moved by up to `PREWARM_JITTER_SECONDS` either way, so jobs
    scheduled for one instant do not all become due in the same second."""
    spread = settings.prewarm_jitter_seconds
    moved = at + timedelta(seconds=random.uniform(-spread, spread))
    return max(moved, not_before) if not_before is not None else moved


# ---------------------------------------------------------------- enqueue


async def enqueue(
    session: AsyncSession,
    kind: JobKind,
    payload: dict[str, Any],
    *,
    priority: int,
    dedupe_key: str,
    source: JobSource,
    run_after: datetime | None = None,
    blocked_by_key: str | None = None,
    run_id: int | None = None,
    max_attempts: int | None = None,
    now: datetime | None = None,
) -> Job:
    """Add a job, or fold this request into the active job with the same key.

    Flushes but does not commit: the caller's transaction decides, so a job
    can be committed atomically with the data that made it necessary.
    """
    job, _ = await enqueue_checked(
        session,
        kind,
        payload,
        priority=priority,
        dedupe_key=dedupe_key,
        source=source,
        run_after=run_after,
        blocked_by_key=blocked_by_key,
        run_id=run_id,
        max_attempts=max_attempts,
        now=now,
    )
    return job


async def enqueue_checked(
    session: AsyncSession,
    kind: JobKind,
    payload: dict[str, Any],
    *,
    priority: int,
    dedupe_key: str,
    source: JobSource,
    run_after: datetime | None = None,
    blocked_by_key: str | None = None,
    run_id: int | None = None,
    max_attempts: int | None = None,
    now: datetime | None = None,
) -> tuple[Job, bool]:
    """`enqueue`, also saying whether a new job was created (True) or the
    request was folded into an existing one (False)."""
    now = now or _now()
    run_after = run_after or now

    existing = await active_job(session, dedupe_key)
    if existing is None:
        job = Job(
            kind=kind,
            payload=payload,
            priority=priority,
            status=JobStatus.QUEUED,
            dedupe_key=dedupe_key,
            blocked_by_key=blocked_by_key,
            run_after=run_after,
            attempts=0,
            max_attempts=max_attempts or settings.job_max_attempts,
            source=source,
            run_id=run_id,
        )
        try:
            # Two enqueuers can both see no active job and both insert. The
            # partial unique index rejects the second; the savepoint keeps
            # that from aborting the caller's transaction.
            async with session.begin_nested():
                session.add(job)
        except IntegrityError:
            existing = await active_job(session, dedupe_key)
            if existing is None:
                raise
        else:
            logger.info(
                "job_enqueued",
                job_id=job.id,
                kind=kind.value,
                dedupe_key=dedupe_key,
                priority=priority,
                source=source.value,
                run_after=run_after.isoformat(),
            )
            await _notify(session, job.id)
            return job, True

    return await _fold_into(session, existing, priority=priority, run_after=run_after), False


async def _fold_into(
    session: AsyncSession, job: Job, *, priority: int, run_after: datetime
) -> Job:
    """Apply a duplicate request to the active job that already covers it."""
    if job.status != JobStatus.QUEUED:
        logger.info("job_enqueue_deduped", job_id=job.id, dedupe_key=job.dedupe_key, status=job.status.value)
        return job

    new_priority = min(job.priority, priority)
    new_run_after = min(_as_utc(job.run_after), run_after)
    if new_priority != job.priority or new_run_after != _as_utc(job.run_after):
        # Conditional: if a worker claimed it a moment ago, leave it alone.
        await session.execute(
            sa.update(Job)
            .where(Job.id == job.id, Job.status == JobStatus.QUEUED)
            .values(priority=new_priority, run_after=new_run_after)
            .execution_options(synchronize_session=False)
        )
        await _notify(session, job.id)
        await session.refresh(job)
        logger.info(
            "job_enqueue_bumped",
            job_id=job.id,
            dedupe_key=job.dedupe_key,
            priority=job.priority,
            run_after=_as_utc(job.run_after).isoformat(),
        )
    else:
        logger.info("job_enqueue_deduped", job_id=job.id, dedupe_key=job.dedupe_key, status=job.status.value)
    return job


async def active_job(session: AsyncSession, dedupe_key: str) -> Job | None:
    """The queued or running job holding `dedupe_key`, if any."""
    return await session.scalar(
        sa.select(Job)
        .where(Job.dedupe_key == dedupe_key, Job.status.in_(ACTIVE_JOB_STATUSES))
        .execution_options(populate_existing=True)
    )


# ------------------------------------------------------------------ claim


async def claim(
    session: AsyncSession,
    worker_id: str,
    *,
    kinds: list[JobKind] | None = None,
    max_priority: int | None = None,
    now: datetime | None = None,
) -> Job | None:
    """Take the next runnable job, or None. Commits, so the claim is visible.

    Runnable means queued, due (`run_after` has passed), not blocked by an
    active job, and within `kinds` and `max_priority` if given.
    """
    now = now or _now()
    blocker = aliased(Job)
    conditions: list[sa.ColumnElement[bool]] = [
        Job.status == JobStatus.QUEUED,
        Job.run_after <= now,
        ~sa.exists().where(
            blocker.dedupe_key == Job.blocked_by_key,
            blocker.status.in_(ACTIVE_JOB_STATUSES),
        ),
    ]
    if kinds is not None:
        conditions.append(Job.kind.in_(kinds))
    if max_priority is not None:
        conditions.append(Job.priority <= max_priority)

    candidate = (
        sa.select(Job.id)
        .where(*conditions)
        .order_by(Job.priority, Job.run_after, Job.id)
        .limit(1)
    )
    values = {
        "status": JobStatus.RUNNING,
        "locked_by": worker_id,
        "locked_at": now,
        "attempts": Job.attempts + 1,
    }

    if _dialect(session) == "postgresql":
        # One statement. SKIP LOCKED makes concurrent claimers step over rows
        # another transaction has already locked instead of waiting on them,
        # so N workers take N different jobs without contention.
        locked = candidate.with_for_update(skip_locked=True, of=Job).scalar_subquery()
        job_id = await session.scalar(
            sa.update(Job).where(Job.id == locked).values(**values).returning(Job.id)
        )
    else:
        # SQLite (the test database) has no row locks and serializes writers,
        # so the portable form is enough: pick a candidate, then claim it with
        # a conditional UPDATE that only succeeds if it is still queued --
        # the same pattern as `ingestion.claim_ingestion`.
        job_id = None
        for _ in range(_SQLITE_CLAIM_ATTEMPTS):
            candidate_id = await session.scalar(candidate)
            if candidate_id is None:
                break
            result = await session.execute(
                sa.update(Job)
                .where(Job.id == candidate_id, Job.status == JobStatus.QUEUED)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount:
                job_id = candidate_id
                break

    if job_id is None:
        await session.commit()
        return None

    await _notify(session, job_id)
    await session.commit()
    job = await session.get(Job, job_id, populate_existing=True)
    assert job is not None
    logger.info(
        "job_claimed",
        job_id=job.id,
        kind=job.kind.value,
        dedupe_key=job.dedupe_key,
        priority=job.priority,
        attempt=job.attempts,
        worker=worker_id,
        waited_s=round((now - _as_utc(job.run_after)).total_seconds(), 1),
    )
    return job


# ------------------------------------------------------------ transitions


async def complete(
    session: AsyncSession,
    job: Job,
    *,
    progress: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    """Mark a claimed job succeeded and commit. False if its lock was lost."""
    now = now or _now()
    values: dict[str, Any] = {"status": JobStatus.SUCCEEDED, "finished_at": now}
    if progress is not None:
        values["progress"] = progress
    if not await _transition(session, job, values):
        return False
    await session.commit()

    logger.info(
        "job_succeeded",
        job_id=job.id,
        kind=job.kind.value,
        dedupe_key=job.dedupe_key,
        attempt=job.attempts,
        duration_s=_duration(job, now),
    )
    await _after_terminal(session, job, now)
    return True


async def fail(
    session: AsyncSession,
    job: Job,
    error: BaseException,
    *,
    now: datetime | None = None,
) -> JobStatus | None:
    """Record a failed attempt and commit: requeue with backoff, or give up.

    Returns the job's new status, or None if its lock was lost.
    """
    now = now or _now()
    message = f"{type(error).__name__}: {error}"[: settings.job_error_max_chars]
    permanent = is_permanent(error)

    if not permanent and job.attempts < job.max_attempts:
        delay = retry_delay(job.attempts)
        status = JobStatus.QUEUED
        values: dict[str, Any] = {
            "status": status,
            "run_after": now + delay,
            "locked_by": None,
            "locked_at": None,
            "last_error": message,
        }
    else:
        status = JobStatus.DEAD
        values = {"status": status, "finished_at": now, "last_error": message}

    if not await _transition(session, job, values):
        return None
    await session.commit()

    if status == JobStatus.QUEUED:
        logger.warning(
            "job_failed",
            job_id=job.id,
            kind=job.kind.value,
            dedupe_key=job.dedupe_key,
            attempt=job.attempts,
            max_attempts=job.max_attempts,
            retry_in_s=round(delay.total_seconds(), 1),
            error=message,
        )
    else:
        logger.error(
            "job_dead",
            job_id=job.id,
            kind=job.kind.value,
            dedupe_key=job.dedupe_key,
            attempt=job.attempts,
            permanent=permanent,
            error=message,
        )
        await _after_terminal(session, job, now)
    return status


async def release(
    session: AsyncSession, job: Job, *, now: datetime | None = None
) -> bool:
    """Hand an unfinished job back to the queue, e.g. on shutdown. Commits.

    The attempt is given back too: the job did not fail, the worker left.
    """
    now = now or _now()
    released = await _transition(
        session,
        job,
        {
            "status": JobStatus.QUEUED,
            "run_after": now,
            "locked_by": None,
            "locked_at": None,
            "attempts": Job.attempts - 1,
        },
    )
    await session.commit()
    if released:
        logger.info("job_released", job_id=job.id, kind=job.kind.value, dedupe_key=job.dedupe_key)
    return released


async def heartbeat(
    session: AsyncSession,
    job: Job,
    *,
    progress: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    """Refresh a running job's lock, optionally recording progress. Commits.

    False means the lock was lost (the job was reaped), and the caller is no
    longer the job's owner.
    """
    values: dict[str, Any] = {"locked_at": now or _now()}
    if progress is not None:
        values["progress"] = progress
    held = await _transition(session, job, values, notify=progress is not None)
    await session.commit()
    return held


async def reap_stale(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Requeue running jobs whose worker stopped refreshing their lock. Commits.

    The orphaned attempt counts: a job that kills its worker every time it
    runs must reach DEAD eventually rather than cycle forever.
    """
    now = now or _now()
    cutoff = now - timedelta(minutes=settings.job_lock_timeout_minutes)
    stale = [Job.status == JobStatus.RUNNING, Job.locked_at < cutoff]
    error = "lock expired: the worker running this job stopped heartbeating"

    dead = (
        await session.execute(
            sa.update(Job)
            .where(*stale, Job.attempts >= Job.max_attempts)
            .values(status=JobStatus.DEAD, finished_at=now, last_error=error)
            .returning(Job.id, Job.run_id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    requeued = (
        await session.execute(
            sa.update(Job)
            .where(*stale)
            .values(
                status=JobStatus.QUEUED,
                run_after=now,
                locked_by=None,
                locked_at=None,
                last_error=error,
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
    ).all()
    for row in [*dead, *requeued]:
        await _notify(session, row.id)
    await session.commit()

    if dead or requeued:
        logger.warning(
            "jobs_reaped",
            requeued=[row.id for row in requeued],
            dead=[row.id for row in dead],
        )
    for run_id in {row.run_id for row in dead if row.run_id is not None}:
        await finish_run_if_done(session, run_id, now=now)
    return len(dead) + len(requeued)


async def _transition(
    session: AsyncSession, job: Job, values: dict[str, Any], *, notify: bool = True
) -> bool:
    """Apply `values` only if this worker still owns the running job."""
    result = await session.execute(
        sa.update(Job)
        .where(
            Job.id == job.id,
            Job.status == JobStatus.RUNNING,
            Job.locked_by == job.locked_by,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        logger.warning("job_lock_lost", job_id=job.id, worker=job.locked_by)
        return False
    if notify:
        await _notify(session, job.id)
    return True


async def _after_terminal(session: AsyncSession, job: Job, now: datetime) -> None:
    # Runs after the commit above, so the check sees this job as terminal.
    # Two last jobs finishing at once both commit first and then check, so at
    # least one of them sees the other finished.
    if job.run_id is not None:
        await finish_run_if_done(session, job.run_id, now=now)


# ------------------------------------------------------- errors and retry


def retry_delay(attempts: int, rng: random.Random | None = None) -> timedelta:
    """Exponential backoff with jitter: base * 2^(attempts - 1), capped."""
    base = settings.job_retry_base_seconds * 2 ** max(attempts - 1, 0)
    capped = min(base, settings.job_retry_max_seconds)
    factor = (rng or random).uniform(1 - RETRY_JITTER_FRACTION, 1 + RETRY_JITTER_FRACTION)
    return timedelta(seconds=capped * factor)


# ------------------------------------------------------------ prewarm runs


BUDGET_TAKEN = "budget_taken"


async def take_enrichment_budget(
    session: AsyncSession, run_id: int, *, job: Job | None = None
) -> bool:
    """Spend one unit of a run's enrichment budget. False if it is exhausted.

    One conditional UPDATE, so jobs from parallel chunks cannot jointly
    overspend. Commits immediately: on Postgres the UPDATE holds the run's row
    lock until commit, and holding it through an enrichment would serialize
    every enrichment in the run behind it.

    With `job`, the job is marked as having paid (`payload["budget_taken"]`)
    in the same transaction, so a retry of that job does not pay twice.
    """
    if job is not None and job.payload.get(BUDGET_TAKEN):
        return True
    result = await session.execute(
        sa.update(PrewarmRun)
        .where(
            PrewarmRun.id == run_id,
            PrewarmRun.enrichments_used < PrewarmRun.enrichment_budget,
        )
        .values(enrichments_used=PrewarmRun.enrichments_used + 1)
        .execution_options(synchronize_session=False)
    )
    taken = bool(result.rowcount)
    if taken and job is not None:
        payload = {**job.payload, BUDGET_TAKEN: True}
        await session.execute(
            sa.update(Job)
            .where(Job.id == job.id)
            .values(payload=payload)
            .execution_options(synchronize_session=False)
        )
        job.payload = payload
    await session.commit()
    return taken


async def add_to_run(session: AsyncSession, run_id: int, **increments: int) -> None:
    """Add to a run's counters atomically, e.g. `movements_found=3`. Flushes.

    Increments rather than assignments, because several price chunks report
    into one run at once.
    """
    values = {
        name: getattr(PrewarmRun, name) + amount
        for name, amount in increments.items()
        if amount
    }
    if values:
        await session.execute(
            sa.update(PrewarmRun)
            .where(PrewarmRun.id == run_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )


async def finish_run_if_done(
    session: AsyncSession, run_id: int, *, now: datetime | None = None
) -> bool:
    """Mark a run finished once none of its jobs are active. Commits."""
    remaining = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Job)
        .where(Job.run_id == run_id, Job.status.in_(ACTIVE_JOB_STATUSES))
    )
    if remaining:
        return False

    now = now or _now()
    result = await session.execute(
        sa.update(PrewarmRun)
        .where(PrewarmRun.id == run_id, PrewarmRun.status == PrewarmRunStatus.RUNNING)
        .values(status=PrewarmRunStatus.FINISHED, finished_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if not result.rowcount:
        return False

    run = await session.get(PrewarmRun, run_id, populate_existing=True)
    assert run is not None
    logger.info(
        "prewarm_run_finished",
        run_id=run.id,
        trading_date=run.trading_date.isoformat(),
        universe_size=run.universe_size,
        movements_found=run.movements_found,
        enrichments_queued=run.enrichments_queued,
        enrichments_used=run.enrichments_used,
        enrichments_deferred=run.enrichments_deferred,
        duration_s=round((now - _as_utc(run.started_at)).total_seconds(), 1),
    )
    return True


# ---------------------------------------------------------------- helpers


async def _notify(session: AsyncSession, job_id: int) -> None:
    """NOTIFY on Postgres, in the caller's transaction; a no-op elsewhere.

    Postgres delivers a notification only when its transaction commits, so a
    listener never hears about a change that was rolled back.
    """
    if _dialect(session) == "postgresql":
        await session.execute(
            sa.text("SELECT pg_notify(:channel, :payload)"),
            {"channel": NOTIFY_CHANNEL, "payload": str(job_id)},
        )


def _dialect(session: AsyncSession) -> str:
    assert session.bind is not None
    return session.bind.dialect.name


def _duration(job: Job, now: datetime) -> float | None:
    if job.locked_at is None:
        return None
    return round((now - _as_utc(job.locked_at)).total_seconds(), 1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres hands back aware ones."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
