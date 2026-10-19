"""GET /events: job progress and data changes as server-sent events, from
Postgres LISTEN or, without it, by polling."""

from __future__ import annotations

import asyncio
import json

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.models.enums import Direction, NewsStatus
from app.models.jobs import Job, JobKind, JobSource
from app.models.market import Movement
from app.services import events, ingestion, queue
from tests.conftest import POSTGRES, postgres_session_factory


def parse(chunks: list[str]) -> list[tuple[str, dict]]:
    out = []
    for block in "".join(chunks).split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if line.startswith(("event: ", "data: ")))
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(settings, "sse_poll_seconds", 0.05)
    monkeypatch.setattr(settings, "sse_heartbeat_seconds", 0.2)
    monkeypatch.setattr(settings, "sse_max_seconds", 1.5)


async def collect(gen, into: list[str]) -> None:
    async for chunk in gen:
        into.append(chunk)


async def wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.02)


async def changes_for(factory, symbol: str, *, use_listen: bool) -> None:
    """Enqueue an ingestion for `symbol`, then change its data, as the worker would."""
    async with factory() as session:
        await queue.enqueue(
            session, JobKind.INGEST_TICKER, {"symbol": symbol}, priority=0,
            dedupe_key=f"ingest:{symbol}", source=JobSource.INTERACTIVE,
        )
        await session.commit()
    async with factory() as session:
        ticker = await ingestion.get_ticker(session, symbol)
        ticker.company_name = "Changed Inc"
        await session.commit()


async def seed_ticker(factory, symbol: str = "EVNT") -> None:
    async with factory() as session:
        await ingestion.get_or_create_ticker(session, symbol)
        await session.commit()


async def test_polling_streams_jobs_data_changes_and_heartbeats(session_factory, fast):
    await seed_ticker(session_factory)
    chunks: list[str] = []
    task = asyncio.create_task(collect(events.stream(session_factory, "EVNT", use_listen=False), chunks))
    await wait_for(lambda: chunks)  # the retry hint is out: the stream is primed
    await changes_for(session_factory, "EVNT", use_listen=False)
    await task

    assert chunks[0] == f"retry: {settings.sse_retry_ms}\n\n"
    received = parse(chunks)
    assert ("job", "ingest_ticker", "queued") in {(t, d.get("kind"), d.get("status")) for t, d in received}
    updated = [d for t, d in received if t == "ticker_updated"]
    assert updated and updated[-1]["symbol"] == "EVNT"
    assert ": ping\n\n" in chunks
    assert events.open_streams == 0


async def test_a_new_stream_starts_with_the_jobs_already_running(session_factory, fast, monkeypatch):
    monkeypatch.setattr(settings, "sse_max_seconds", 0.2)
    await seed_ticker(session_factory)
    async with session_factory() as session:
        await queue.enqueue(session, JobKind.INGEST_TICKER, {"symbol": "EVNT"}, priority=0,
                            dedupe_key="ingest:EVNT", source=JobSource.INTERACTIVE)
        await session.commit()
    chunks: list[str] = []
    await collect(events.stream(session_factory, "EVNT", use_listen=False), chunks)
    assert [(t, d["status"]) for t, d in parse(chunks)] == [("job", "queued")]


async def test_a_movements_enrichment_is_its_tickers_job(session_factory):
    from datetime import date, datetime, timezone

    await seed_ticker(session_factory)
    async with session_factory() as session:
        ticker = await ingestion.get_ticker(session, "EVNT")
        movement = Movement(
            ticker_id=ticker.id, date=date(2026, 1, 5), daily_return=0.05, abs_return=0.05,
            direction=Direction.UP, prev_adj_close=100, adj_close=105, threshold=0.03,
            threshold_source="floor", detector_k=2.0, detector_window=20, detector_floor=0.03,
            news_status=NewsStatus.PENDING, news_window_closes_at=datetime(2026, 1, 8, tzinfo=timezone.utc),
        )
        session.add(movement)
        await session.flush()
        job = Job(kind=JobKind.ENRICH_MOVEMENT, payload={"movement_id": movement.id})
        assert await events.job_symbols(session, job) == {"EVNT"}
        assert await events.job_symbols(session, Job(kind=JobKind.REFRESH_PRICES, payload={"symbols": ["A", "B"]})) == {"A", "B"}
        assert await events.job_symbols(session, Job(kind=JobKind.SCHEDULE_NIGHTLY, payload={})) == set()


async def test_the_route_streams_with_proxy_safe_headers(client, fast, monkeypatch, session_factory):
    assert not events.listens(session_factory)  # SQLite polls
    monkeypatch.setattr(settings, "sse_max_seconds", 0.3)
    await client.get("/tickers/AAPL")  # cold: queues an ingestion
    response = await client.get("/events", params={"symbol": "aapl"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no" and response.headers["cache-control"] == "no-cache"
    assert [(t, d["kind"]) for t, d in parse([response.text])] == [("job", "ingest_ticker")]


async def test_the_route_refuses_past_the_connection_cap_and_bad_symbols(client, monkeypatch):
    monkeypatch.setattr(events, "open_streams", settings.sse_max_connections)
    capped = await client.get("/events", params={"symbol": "AAPL"})
    assert capped.status_code == 503 and capped.headers["retry-after"] == "5"
    monkeypatch.setattr(events, "open_streams", 0)
    assert (await client.get("/events", params={"symbol": "not a symbol!"})).status_code == 422


async def test_a_full_queue_drops_the_oldest_event_not_the_stream(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "sse_queue_size", 2)
    hub = events.Broker(session_factory)
    monkeypatch.setattr(hub, "_ensure_started", lambda: None)
    q = hub.subscribe("EVNT")
    await seed_ticker(session_factory)
    async with session_factory() as session:
        ticker_id = (await ingestion.get_ticker(session, "EVNT")).id
    for _ in range(3):
        await hub._dispatch(events.TICKER_CHANNEL, str(ticker_id))
    assert q.qsize() == 2
    hub.unsubscribe("EVNT", q)
    assert "EVNT" not in hub.subscribers


@pytest.mark.parametrize("_pg", [pytest.param(None, marks=POSTGRES)])
async def test_postgres_listen_delivers_committed_changes(_pg, fast):
    async with postgres_session_factory() as factory:
        assert events.listens(factory)
        await seed_ticker(factory)
        chunks: list[str] = []
        task = asyncio.create_task(collect(events.stream(factory, "EVNT", use_listen=True), chunks))
        await wait_for(lambda: events._broker is not None and events._broker.listening)
        await changes_for(factory, "EVNT", use_listen=True)
        await wait_for(lambda: any("ticker_updated" in c for c in chunks))
        await task
        await events.close_broker()
        received = parse(chunks)
        assert ("job", "queued") in {(t, d.get("status")) for t, d in received}
        assert any(t == "ticker_updated" and d["symbol"] == "EVNT" for t, d in received)
        async with factory() as session:
            assert await session.scalar(sa.select(sa.func.count()).select_from(Job)) == 1
