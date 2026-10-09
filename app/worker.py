"""The queue worker: `python -m app.worker`.

One process runs `WORKER_CONCURRENCY` claim loops. Each loop claims a job,
runs its handler in a fresh session, and records the outcome; when nothing is
claimable it sleeps for the poll interval plus jitter. A separate task reaps
jobs orphaned by dead workers, and each running job has a heartbeat task that
keeps its lock fresh and publishes the handler's progress.

The interactive lane
--------------------
Priority alone does not keep a user from waiting. If every loop is busy with a
30-second nightly enrichment, a priority-0 job is first in line but still
waits for one to finish. So `WORKER_INTERACTIVE_SLOTS` of the loops only ever
claim priority-0 jobs and are idle otherwise. The remaining loops claim
anything, priority 0 included, so interactive work drains faster still when
there is no nightly backlog.

The nightly schedule
--------------------
Each worker also runs a small scheduler. Whenever it wakes, it enqueues the
most recent scheduled run (`schedule.latest_run_at`) unless that trading date
already has one, then sleeps until the next. Waking for the most recent run,
rather than only at the exact instant, means a worker that was down at 17:15
catches up when it starts. Every replica may do this: the job is deduplicated
on `nightly:{date}`, and the run itself is unique per trading date, so a date
is never run twice.

Spending
--------
Each job runs with its attribution set (`app.core.context`): its id, and the
call class the spend cap draws on, `interactive` for priority 0 and
`background` otherwise. A job the cap refuses is held until the cap resets
(`queue.hold`), not failed: it keeps its attempts and its movement stays as it
was.

Shutdown
--------
On SIGTERM or SIGINT the loops stop claiming and in-flight jobs get
`WORKER_SHUTDOWN_TIMEOUT_SECONDS` to finish. Whatever is still running then is
cancelled and released back to the queue with its attempt refunded, so a
deploy never costs a job a retry. Jobs a killed process could not release are
picked up by another worker's reaper once their lock expires.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import signal
import socket
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.context import CallClass, attributed
from app.core.errors import SpendCapReached
from app.core.logging import configure_logging, get_logger
from app.db.session import SessionLocal, dispose_engine
from app.models.jobs import Job, JobKind, JobSource
from app.services import prewarm, queue, schedule, spend
from app.services.job_handlers import HANDLERS, Handler, JobContext
from app.services.llm import LLMProvider, get_llm_client

logger = get_logger(__name__)

# Longest single sleep of the scheduler loop.
SCHEDULER_MAX_SLEEP_SECONDS = 300.0


def call_class_for(job: Job) -> CallClass:
    """Priority 0 means a user is waiting on the job; everything else is background.

    Read at run time, so a nightly job that a user's request pulled forward to
    priority 0 spends as interactive, as it does for the nightly budget.
    """
    if job.priority <= queue.PRIORITY_INTERACTIVE:
        return CallClass.INTERACTIVE
    return CallClass.BACKGROUND


class Worker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
        handlers: dict[JobKind, Handler] | None = None,
        llm: LLMProvider | None = None,
        concurrency: int | None = None,
        interactive_slots: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._handlers = handlers if handlers is not None else HANDLERS
        self._llm = llm
        self._concurrency = concurrency or settings.worker_concurrency
        self._interactive_slots = (
            settings.worker_interactive_slots
            if interactive_slots is None
            else interactive_slots
        )
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        )
        self._stopping = asyncio.Event()

    # ---------------------------------------------------------------- run

    async def run(self) -> None:
        """Process jobs until `stop()` is called, then shut down gracefully."""
        logger.info(
            "worker_started",
            worker=self.worker_id,
            concurrency=self._concurrency,
            interactive_slots=self._interactive_slots,
            kinds=[kind.value for kind in self._handlers],
        )
        loops = [
            asyncio.create_task(self._loop(interactive=i < self._interactive_slots))
            for i in range(self._concurrency)
        ]
        background = [asyncio.create_task(self._reap_loop())]
        if settings.prewarm_schedule_enabled and JobKind.SCHEDULE_NIGHTLY in self._handlers:
            background.append(asyncio.create_task(self._schedule_loop()))

        await self._stopping.wait()
        for task in background:
            task.cancel()
        _, unfinished = await asyncio.wait(
            loops, timeout=settings.worker_shutdown_timeout_seconds
        )
        for task in unfinished:
            task.cancel()  # each loop releases its own job on cancellation
        await asyncio.gather(*loops, *background, return_exceptions=True)
        logger.info("worker_stopped", worker=self.worker_id, released=len(unfinished))

    def stop(self) -> None:
        logger.info("worker_stopping", worker=self.worker_id)
        self._stopping.set()

    async def run_once(self, *, interactive: bool = False) -> Job | None:
        """Claim and run a single job. Returns it, or None if none was due."""
        job = await self._claim(interactive)
        if job is not None:
            await self._execute(job)
        return job

    # -------------------------------------------------------------- loops

    async def _loop(self, *, interactive: bool) -> None:
        while not self._stopping.is_set():
            try:
                job = await self._claim(interactive)
            except Exception as exc:  # database unreachable, most likely
                logger.error("worker_claim_failed", worker=self.worker_id, error=str(exc))
                job = None
            if job is None:
                await self._idle()
                continue
            try:
                await self._execute(job)
            except asyncio.CancelledError:
                await self._release(job)
                raise
            except Exception as exc:
                # Recording the outcome itself failed; the reaper will requeue it.
                logger.error("worker_record_failed", job_id=job.id, error=str(exc))

    async def _claim(self, interactive: bool) -> Job | None:
        async with self._session_factory() as session:
            return await queue.claim(
                session,
                self.worker_id,
                kinds=list(self._handlers),
                max_priority=queue.PRIORITY_INTERACTIVE if interactive else None,
            )

    async def _execute(self, job: Job) -> None:
        context = JobContext(job=job, llm=self._llm or get_llm_client())
        keepalive = asyncio.create_task(self._keepalive(job, context))
        try:
            async with self._session_factory() as session:
                try:
                    with attributed(
                        call_class=call_class_for(job), job_id=job.id, user_id=None
                    ):
                        await self._handlers[job.kind](session, job, context)
                except SpendCapReached as exc:
                    await session.rollback()
                    await self._hold_for_spend_cap(session, job, exc)
                    return
                except Exception as exc:
                    await session.rollback()
                    error: Exception | None = exc
                else:
                    await queue.complete(session, job, progress=context.progress)
                    return
            async with self._session_factory() as session:
                await queue.fail(session, job, error)
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive

    async def _hold_for_spend_cap(
        self, session: AsyncSession, job: Job, refusal: SpendCapReached
    ) -> None:
        """Hold the job until the cap resets, spread out after midnight.

        A nightly enrichment held this way counts as deferred on its run, like
        one the run's own budget turned away.
        """
        until = queue.jittered_after(refusal.retry_at, settings.spend_resume_jitter_seconds)
        if not await queue.hold(
            session, job, until=until, reason=queue.HOLD_SPEND_CAP, detail=str(refusal)
        ):
            return
        run_id = job.payload.get("run_id")
        if job.kind == JobKind.ENRICH_MOVEMENT and run_id is not None:
            await queue.add_to_run(session, int(run_id), enrichments_deferred=1)
            await session.commit()

    async def _keepalive(self, job: Job, context: JobContext) -> None:
        """Refresh the job's lock every heartbeat, and publish progress when
        the handler reports some. Best-effort: a failed write is retried on
        the next round, and never reaches the handler."""
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    context.progress_changed.wait(),
                    timeout=settings.job_heartbeat_seconds,
                )
            changed = context.progress_changed.is_set()
            context.progress_changed.clear()
            try:
                async with self._session_factory() as session:
                    held = await queue.heartbeat(
                        session, job, progress=context.progress if changed else None
                    )
            except Exception as exc:
                logger.warning("job_heartbeat_failed", job_id=job.id, error=str(exc))
                continue
            if not held:
                # Reaped while still running here: another worker may now run
                # it too. Handlers are idempotent, so this is wasted work, not
                # corruption; the outcome of whichever finishes last is dropped.
                return

    async def _reap_loop(self) -> None:
        while True:
            try:
                async with self._session_factory() as session:
                    await queue.reap_stale(session)
            except Exception as exc:
                logger.error("worker_reap_failed", error=str(exc))
            await asyncio.sleep(settings.job_reap_interval_seconds)

    async def schedule_tick(self, now: datetime | None = None) -> Job | None:
        """Enqueue the most recent scheduled run if its date has no run yet."""
        now = now or datetime.now(timezone.utc)
        due = schedule.latest_run_at(now)
        trading_date = schedule.trading_date(due)
        async with self._session_factory() as session:
            if await prewarm.run_for(session, trading_date) is not None:
                return None
            job = await queue.enqueue(
                session,
                JobKind.SCHEDULE_NIGHTLY,
                {"trading_date": trading_date.isoformat()},
                priority=queue.PRIORITY_NIGHTLY_FANOUT,
                dedupe_key=queue.nightly_key(trading_date),
                source=JobSource.SCHEDULED,
                run_after=queue.jittered(due),
                now=now,
            )
            await session.commit()
            return job

    async def _schedule_loop(self) -> None:
        while not self._stopping.is_set():
            now = datetime.now(timezone.utc)
            try:
                await self.schedule_tick(now)
            except Exception as exc:
                logger.error("schedule_tick_failed", error=str(exc))
            until_next = (schedule.next_run_after(now) - now).total_seconds()
            # Sleep in bounded steps, so a changed system clock is noticed.
            delay = min(max(until_next, 0.0), SCHEDULER_MAX_SLEEP_SECONDS)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)

    async def _release(self, job: Job) -> None:
        try:
            async with self._session_factory() as session:
                await queue.release(session, job)
        except Exception as exc:
            logger.error("job_release_failed", job_id=job.id, error=str(exc))

    async def _idle(self) -> None:
        delay = settings.worker_poll_interval_seconds + random.uniform(
            0, settings.worker_poll_jitter_seconds
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=delay)


async def _main() -> None:
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        await dispose_engine()


def main() -> None:
    configure_logging()
    spend.warn_on_startup()
    asyncio.run(_main())


if __name__ == "__main__":
    main()
