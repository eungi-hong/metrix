"""Live updates for one ticker, as server-sent events.

Two event types, both JSON:

* `job`: a job about the ticker changed (`id`, `kind`, `status`, `progress`):
  its ingestion, a price refresh that includes it, or one of its movements'
  enrichments;
* `ticker_updated`: a commit changed what `GET /tickers/{symbol}` shows
  (`symbol`, `data_version`), so a client refetches (and its ETag will have
  changed).

Where they come from:

* **Postgres:** one `LISTEN` connection per process, on `job_events` (sent
  by the queue on every transition) and `ticker_events` (sent by the
  data-version hook, `app.db.versions`). Notifications are resolved to a
  symbol and fanned out to that symbol's subscribers. Postgres delivers a
  NOTIFY only once its transaction commits, so nothing uncommitted is seen.
* **Polling fallback:** without Postgres, or while the LISTEN connection is
  down, each stream reads the ticker's data version and its recent jobs
  every `SSE_POLL_SECONDS` and sends what changed.

A stream sends a `retry` hint first, a comment line as a heartbeat every
`SSE_HEARTBEAT_SECONDS`, and ends after `SSE_MAX_SECONDS` (the client
reconnects), so no proxy or load balancer holds it open forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import metrics
from app.core.config import settings
from app.core.logging import get_logger
from app.db.versions import NOTIFY_CHANNEL as TICKER_CHANNEL
from app.models.jobs import ACTIVE_JOB_STATUSES, Job
from app.models.market import Movement, Ticker
from app.services.queue import NOTIFY_CHANNEL as JOB_CHANNEL

logger = get_logger(__name__)

# A finished job stays interesting to a new stream for this long (polling).
RECENT_JOBS = timedelta(minutes=10)
# Streams open in this process (the cap is SSE_MAX_CONNECTIONS).
open_streams = 0


@dataclass(frozen=True, slots=True)
class Event:
    type: str  # "job" or "ticker_updated"
    data: dict[str, Any]

    def encode(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.data, default=str, separators=(',', ':'))}\n\n"


def job_event(job: Job) -> Event:
    return Event(
        "job",
        {
            "id": job.id,
            "kind": str(getattr(job.kind, "value", job.kind)),
            "status": str(getattr(job.status, "value", job.status)),
            "progress": job.progress,
        },
    )


async def job_symbols(session: AsyncSession, job: Job) -> set[str]:
    """The tickers a job is about, from its payload."""
    payload = job.payload or {}
    symbols: set[str] = set()
    if isinstance(payload.get("symbol"), str):
        symbols.add(payload["symbol"])
    if isinstance(payload.get("symbols"), list):
        symbols.update(s for s in payload["symbols"] if isinstance(s, str))
    if payload.get("movement_id") is not None:
        symbol = await session.scalar(
            sa.select(Ticker.symbol)
            .join(Movement, Movement.ticker_id == Ticker.id)
            .where(Movement.id == int(payload["movement_id"]))
        )
        if symbol:
            symbols.add(symbol)
    return symbols


# --------------------------------------------------------------- fan-out ---


class Broker:
    """One LISTEN connection per process, fanned out to every stream."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self.subscribers: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)
        self.inbox: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.connected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def listening(self) -> bool:
        return self.connected.is_set()

    def subscribe(self, symbol: str) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=settings.sse_queue_size)
        self.subscribers[symbol].add(queue)
        self._ensure_started()
        return queue

    def unsubscribe(self, symbol: str, queue: asyncio.Queue[Event]) -> None:
        self.subscribers[symbol].discard(queue)
        if not self.subscribers[symbol]:
            self.subscribers.pop(symbol, None)

    def _ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="sse-listen")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self.connected.clear()

    async def _run(self) -> None:
        """Hold the LISTEN connection, reconnecting with backoff, and dispatch."""
        import asyncpg  # Postgres only

        dsn = _engine_url(self.session_factory).render_as_string(hide_password=False).replace("+asyncpg", "")
        delay = 1.0
        while True:
            connection = None
            try:
                connection = await asyncpg.connect(dsn, timeout=10)

                def received(_conn, _pid, channel: str, payload: str) -> None:
                    self.inbox.put_nowait((channel, payload))

                await connection.add_listener(JOB_CHANNEL, received)
                await connection.add_listener(TICKER_CHANNEL, received)
                self.connected.set()
                delay = 1.0
                while not connection.is_closed():
                    try:
                        channel, payload = await asyncio.wait_for(self.inbox.get(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    if self.subscribers:
                        await self._dispatch(channel, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("sse_listen_failed", error=str(exc), retry_in=delay)
            finally:
                self.connected.clear()
                if connection is not None:
                    with contextlib.suppress(Exception):
                        await connection.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _dispatch(self, channel: str, payload: str) -> None:
        try:
            ident = int(payload)
        except ValueError:
            return
        async with self.session_factory() as session:
            if channel == TICKER_CHANNEL:
                row = (
                    await session.execute(sa.select(Ticker.symbol, Ticker.data_version).where(Ticker.id == ident))
                ).first()
                if row is None:
                    return
                targets = {row.symbol}
                event = Event("ticker_updated", {"symbol": row.symbol, "data_version": row.data_version})
            else:
                job = await session.get(Job, ident)
                if job is None:
                    return
                targets = await job_symbols(session, job)
                event = job_event(job)
        for symbol in targets:
            for queue in list(self.subscribers.get(symbol, ())):
                if queue.full():  # a stalled reader loses events, never blocks others
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(event)


_broker: Broker | None = None


def _engine_url(session_factory: async_sessionmaker[AsyncSession]):
    return session_factory.kw["bind"].url


def listens(session_factory: async_sessionmaker[AsyncSession]) -> bool:
    """LISTEN needs Postgres; anything else polls."""
    return _engine_url(session_factory).get_backend_name() == "postgresql"


def broker(session_factory: async_sessionmaker[AsyncSession]) -> Broker:
    """The process's broker (one in production; tests may switch databases)."""
    global _broker
    if _broker is None or _broker.session_factory is not session_factory:
        if _broker is not None:
            asyncio.get_running_loop().create_task(_broker.close())
        _broker = Broker(session_factory)
    return _broker


async def close_broker() -> None:
    global _broker
    if _broker is not None:
        await _broker.close()
    _broker = None


# ---------------------------------------------------------------- polling ---


class Poller:
    """Fallback: what changed for one symbol since the last look."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], symbol: str) -> None:
        self.session_factory = session_factory
        self.symbol = symbol
        self.version: int | None = None
        self.jobs: dict[int, tuple[str, str]] = {}

    async def changes(self, *, first: bool = False) -> list[Event]:
        events: list[Event] = []
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            version = await session.scalar(sa.select(Ticker.data_version).where(Ticker.symbol == self.symbol))
            if version is not None and self.version is not None and version != self.version:
                events.append(Event("ticker_updated", {"symbol": self.symbol, "data_version": version}))
            self.version = version
            candidates = (
                await session.scalars(
                    sa.select(Job).where(
                        sa.or_(
                            Job.status.in_(ACTIVE_JOB_STATUSES),
                            Job.finished_at >= now - RECENT_JOBS,
                        )
                    )
                )
            ).all()
            for job in candidates:
                if self.symbol not in await job_symbols(session, job):
                    continue
                key = (str(job.status), json.dumps(job.progress, sort_keys=True, default=str))
                if self.jobs.get(job.id) != key:
                    if not first or job.status in ACTIVE_JOB_STATUSES:
                        events.append(job_event(job))
                    self.jobs[job.id] = key
        return events


# ----------------------------------------------------------------- stream ---


async def stream(
    session_factory: async_sessionmaker[AsyncSession],
    symbol: str,
    *,
    use_listen: bool | None = None,
) -> AsyncIterator[str]:
    """The text/event-stream body for one subscriber."""
    if use_listen is None:
        use_listen = listens(session_factory)
    global open_streams
    open_streams += 1
    metrics.SSE_OPEN.inc()
    hub = broker(session_factory) if use_listen else None
    queue = hub.subscribe(symbol) if hub else None
    poller = Poller(session_factory, symbol)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.sse_max_seconds
    try:
        yield f"retry: {settings.sse_retry_ms}\n\n"
        # The current state first, so a client that connects mid-ingest
        # knows where things stand without a separate request.
        for event in await poller.changes(first=True):
            yield event.encode()
        next_beat = loop.time() + settings.sse_heartbeat_seconds
        while (now := loop.time()) < deadline:
            wait = min(next_beat, deadline) - now
            if hub is not None and hub.listening:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=max(0.0, wait))
                    yield event.encode()
                    continue
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(max(0.0, min(wait, settings.sse_poll_seconds)))
                for event in await poller.changes():
                    yield event.encode()
            if loop.time() >= next_beat:
                yield ": ping\n\n"
                next_beat = loop.time() + settings.sse_heartbeat_seconds
    finally:
        open_streams -= 1
        metrics.SSE_OPEN.dec()
        if hub is not None and queue is not None:
            hub.unsubscribe(symbol, queue)
