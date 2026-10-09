"""Test fixtures.

The endpoint tests run against an in-memory SQLite database with stubbed
external services. That keeps `pytest` a single command with no Docker, no
network, and no API keys, which matters more for a reviewer than exercising
Postgres-specific SQL -- the models are declared portably and the production
path is covered by the migration running under docker-compose.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models.jobs import Job
from app.services import prices as price_service
from app.services import ratelimit
from app.services.llm import LLMProvider, get_llm_client
from app.services.peers import PeerSet
from app.services.prices import PriceBarData, PriceHistory, TickerProfile
from app.services.relevance import ArticleAssessment, RelevanceReport

START = date(2024, 1, 2)


# --------------------------------------------------------------------- stubs


class StubLLM(LLMProvider):
    """Deterministic stand-in for a real provider.

    Returns a valid instance of whatever schema it is asked for, and records
    every call so tests can assert on what the pipeline actually sent.
    """

    name = "stub"

    def __init__(self) -> None:
        self.model = "stub-model"
        self.structured_calls: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []
        self.answer = "The move was driven by the reported earnings miss [M1] [A1]."

    @property
    def available(self) -> bool:
        return True

    async def parse_structured(
        self, *, system: str, user: str, output_model: type[BaseModel], **kwargs: Any
    ) -> Any:
        self.structured_calls.append({"system": system, "user": user})

        if output_model is PeerSet:
            return PeerSet(
                peers=[{"name": "Rival Corp", "ticker": "RVL", "relationship": "competitor"}],
                industry_themes=["widget pricing"],
            )
        if output_model is RelevanceReport:
            # Score the first article as company news, the second as macro, and
            # reject the rest -- enough to exercise tiering and filtering.
            count = user.count("] title:")
            assessments = []
            for index in range(count):
                if index == 0:
                    assessments.append(
                        ArticleAssessment(
                            index=index,
                            tier="easy",
                            score=0.91,
                            rationale="Reports the earnings miss on the day of the move.",
                        )
                    )
                elif index == 1:
                    assessments.append(
                        ArticleAssessment(
                            index=index,
                            tier="hard",
                            score=0.62,
                            rationale="Rate decision repriced the whole sector.",
                        )
                    )
                else:
                    assessments.append(
                        ArticleAssessment(
                            index=index,
                            tier="irrelevant",
                            score=0.05,
                            rationale="Unrelated coverage.",
                        )
                    )
            return RelevanceReport(assessments=assessments)

        raise AssertionError(f"StubLLM has no canned response for {output_model}")

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> str:
        self.complete_calls.append({"system": system, "messages": messages})
        return self.answer


def build_price_history(symbol: str = "TEST") -> PriceHistory:
    """A stock with a real volatility regime and one unmistakable shock day.

    Daily moves alternate +/-1.5%, so the rolling sigma is ~0.015 and the
    volatility term (2 * sigma = ~3%) binds above the 2% floor. The ordinary
    days sit below that bar and the -8% day clears it at ~5 sigma, which makes
    the expected result exactly one movement, found by the volatility term
    rather than the floor.
    """
    bars: list[PriceBarData] = []
    price = 100.0
    for offset in range(40):
        price *= 1.015 if offset % 2 else 0.985
        bars.append(_bar(START + timedelta(days=offset), price))
    price *= 0.92  # the movement
    bars.append(_bar(START + timedelta(days=40), price))

    return PriceHistory(
        profile=TickerProfile(
            symbol=symbol,
            company_name="Test Industries Inc",
            sector="Technology",
            industry="Widgets",
            exchange="NASDAQ",
            currency="USD",
        ),
        bars=bars,
    )


def _bar(day: date, price: float) -> PriceBarData:
    return PriceBarData(
        date=day,
        open=price,
        high=price * 1.01,
        low=price * 0.99,
        close=price,
        adj_close=price,
        volume=1_000_000,
    )


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def stub_llm() -> StubLLM:
    return StubLLM()


@pytest.fixture(autouse=True)
def fresh_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unthrottled, fresh buckets per test.

    Fresh because a bucket holds an asyncio lock, which must not outlive a
    test's event loop; unthrottled because no test but the rate-limit tests
    should spend time waiting for tokens.
    """
    for name in ("exa_max_rps", "anthropic_max_rpm", "yfinance_max_rps"):
        monkeypatch.setattr(settings, name, 1e9)
    ratelimit.reset()


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network in tests: fixture news provider, stubbed price fetch."""
    monkeypatch.setattr(settings, "news_provider", "fixture")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    async def fake_fetch(symbol: str, days: int | None = None) -> PriceHistory:
        return build_price_history(symbol)

    monkeypatch.setattr(price_service, "fetch_price_history", fake_fetch)


@asynccontextmanager
async def sqlite_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A fresh in-memory database with every table created."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,  # one shared connection, so ":memory:" persists
        connect_args={"check_same_thread": False},
    )
    _use_real_sqlite_transactions(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

# Marks for a test, or a fixture parameter, that needs real Postgres.
POSTGRES = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set"),
]


@asynccontextmanager
async def postgres_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The TEST_DATABASE_URL database, wiped and recreated.

    Refuses any database without "test" in its name, so pointing it at real
    data by mistake fails instead of dropping every table.
    """
    assert TEST_DATABASE_URL is not None
    if "test" not in (make_url(TEST_DATABASE_URL).database or ""):
        pytest.fail("TEST_DATABASE_URL must name a throwaway database containing 'test'")

    engine = create_async_engine(TEST_DATABASE_URL, pool_size=20)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    async with sqlite_session_factory() as factory:
        yield factory


def _use_real_sqlite_transactions(engine, *, immediate: bool = False) -> None:
    """Make SQLite begin transactions when SQLAlchemy does, not on first write.

    The sqlite3 driver defers BEGIN until the first INSERT/UPDATE, so a
    SAVEPOINT issued before any write opens the transaction itself and its
    RELEASE commits it -- a rolled-back enqueue would survive. Postgres has no
    such quirk. This is SQLAlchemy's documented recipe for it.

    `immediate` takes the write lock at BEGIN. With several connections, a
    WAL transaction that starts reading and later writes fails at once if
    another connection committed in between ("database is locked"), where
    Postgres would simply proceed; taking the lock up front makes the other
    connection wait instead.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _no_driver_transactions(dbapi_connection, _record) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin(connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")


@pytest.fixture
async def db(tmp_path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """A file-backed SQLite database with a connection per session.

    For tests where two sessions write concurrently (a worker's keepalive
    beside its handler), which the shared in-memory connection cannot do.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}",
        poolclass=NullPool,
        connect_args={"timeout": 10},  # wait for a lock rather than fail
    )
    _use_real_sqlite_transactions(engine, immediate=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _wal(dbapi_connection, _record) -> None:
        # Readers do not block on the writer, much like Postgres.
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def session(session_factory) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def client(
    session_factory, stub_llm: StubLLM
) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_llm_client] = lambda: stub_llm

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client


async def all_jobs(session_factory: async_sessionmaker[AsyncSession]) -> list[Job]:
    """Every job in the queue, oldest first."""
    async with session_factory() as session:
        return list((await session.scalars(sa.select(Job).order_by(Job.id))).all())


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc)
