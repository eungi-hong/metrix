"""Queue behaviour that only real Postgres can show: row locks, SKIP LOCKED and
NOTIFY. (Every test in test_queue.py also runs against Postgres when it is
available; these are the ones that need real concurrency.)

Skipped unless TEST_DATABASE_URL is set. The database is wiped, so the URL
must name a database with "test" in its name, e.g.

    TEST_DATABASE_URL=postgresql+asyncpg://metrix:metrix@localhost:5433/metrix_test pytest -m postgres
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import date

import asyncpg
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.jobs import Job, JobKind, JobSource, PrewarmRun
from app.services import queue
from tests.conftest import (
    POSTGRES,
    TEST_DATABASE_URL,
    postgres_session_factory,
)

pytestmark = POSTGRES


@pytest.fixture
async def pg() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    async with postgres_session_factory() as factory:
        yield factory


async def test_concurrent_claimers_never_take_the_same_job(pg):
    async with pg() as session:
        for i in range(60):
            await queue.enqueue(
                session, JobKind.ENRICH_MOVEMENT, {}, priority=i % 3,
                dedupe_key=f"enrich:{i}", source=JobSource.SCHEDULED,
            )
        await session.commit()

    async def claimer(name: str) -> list[int]:
        taken: list[int] = []
        async with pg() as session:
            while (job := await queue.claim(session, name)) is not None:
                taken.append(job.id)
        return taken

    results = await asyncio.gather(*(claimer(f"w{i}") for i in range(8)))
    claimed = [job_id for taken in results for job_id in taken]

    assert len(claimed) == 60
    assert len(set(claimed)) == 60, "a job was claimed twice"
    assert sum(1 for taken in results if taken) > 1, "the claimers did not overlap"


async def test_concurrent_spenders_never_exceed_the_budget(pg):
    async with pg() as session:
        run = PrewarmRun(trading_date=date(2026, 10, 9), enrichment_budget=25)
        session.add(run)
        await session.commit()

    async def spender() -> int:
        async with pg() as session:
            return sum([await queue.take_enrichment_budget(session, run.id) for _ in range(10)])

    spent = await asyncio.gather(*(spender() for _ in range(8)))

    assert sum(spent) == 25
    async with pg() as session:
        assert (await session.get(PrewarmRun, run.id)).enrichments_used == 25


async def test_concurrent_enqueues_of_one_key_make_one_job(pg):
    async def enqueuer(priority: int) -> int:
        async with pg() as session:
            job = await queue.enqueue(
                session, JobKind.INGEST_TICKER, {"symbol": "NVDA"}, priority=priority,
                dedupe_key="ingest:NVDA", source=JobSource.INTERACTIVE,
            )
            await session.commit()
            return job.id

    ids = await asyncio.gather(*(enqueuer(p) for p in range(10)))

    async with pg() as session:
        count = await session.scalar(sa.select(sa.func.count()).select_from(Job))
    assert count == 1
    assert len(set(ids)) == 1


async def test_status_changes_are_notified_on_commit(pg):
    url = make_url(TEST_DATABASE_URL).set(drivername="postgresql")
    listener = await asyncpg.connect(url.render_as_string(hide_password=False))
    received: asyncio.Queue[str] = asyncio.Queue()
    await listener.add_listener(queue.NOTIFY_CHANNEL, lambda *args: received.put_nowait(args[-1]))
    try:
        async with pg() as session:
            job = await queue.enqueue(
                session, JobKind.INGEST_TICKER, {}, priority=0,
                dedupe_key="ingest:X", source=JobSource.INTERACTIVE,
            )
            await session.commit()
            await queue.claim(session, "w1")

        assert await asyncio.wait_for(received.get(), timeout=5) == str(job.id)
        assert await asyncio.wait_for(received.get(), timeout=5) == str(job.id)
    finally:
        await listener.close()
