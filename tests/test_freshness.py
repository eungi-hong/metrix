"""Tests for news freshness: PARTIAL enrichment, idempotent re-enrichment, and
the attempt cap on movements that keep failing.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.core.errors import NewsProviderError
from app.models.enums import NewsStatus
from app.models.market import Movement
from app.models.news import MovementNewsLink, NewsArticle
from app.services import ingestion
from app.services import prices as price_service
from app.services.news.base import NewsCandidate, NewsProvider, NewsSearchRequest
from app.services.news.fixture import FixtureNewsProvider
from app.services.news.queries import news_window_closes_at
from tests.conftest import build_price_history


def history_ending_on(last_day: date, symbol: str = "TEST"):
    """The conftest series, shifted so its shock day is `last_day`."""
    history = build_price_history(symbol)
    shift = last_day - history.bars[-1].date
    return dataclasses.replace(
        history,
        bars=[dataclasses.replace(bar, date=bar.date + shift) for bar in history.bars],
    )


@pytest.fixture
def recent_move(monkeypatch: pytest.MonkeyPatch) -> date:
    """Price data whose movement is today, so its news window is still open."""
    today = datetime.now(timezone.utc).date()

    async def fetch(symbol: str, days: int | None = None):
        return history_ending_on(today, symbol)

    monkeypatch.setattr(price_service, "fetch_price_history", fetch)
    return today


class ScriptedProvider(NewsProvider):
    """Returns `easy_urls` for the company search and nothing for the others."""

    name = "scripted"

    def __init__(self, published: date) -> None:
        self.easy_urls: list[str] = []
        self.published = published

    async def execute(self, request: NewsSearchRequest) -> dict[str, Any]:
        urls = self.easy_urls if "company news" in request.query else []
        return {"results": [{"url": url, "title": url} for url in urls]}

    def parse(self, raw: dict[str, Any]) -> list[NewsCandidate]:
        published = datetime.combine(self.published, datetime.min.time(), tzinfo=timezone.utc)
        return [
            NewsCandidate(url=item["url"], title=item["title"], published_at=published, provider=self.name)
            for item in raw["results"]
        ]


class FailingProvider(NewsProvider):
    name = "failing"

    async def execute(self, request: NewsSearchRequest) -> dict[str, Any]:
        raise NewsProviderError(self.name, "down")

    def parse(self, raw: dict[str, Any]) -> list[NewsCandidate]:
        return []


async def only_movement(session) -> Movement:
    session.expire_all()
    return (await session.scalars(sa.select(Movement))).one()


@pytest.fixture
def advance_clock(monkeypatch: pytest.MonkeyPatch):
    """Move ingestion's clock forward, e.g. past a movement's window close."""

    def advance(by: timedelta) -> None:
        later = datetime.now(timezone.utc) + by
        monkeypatch.setattr(ingestion, "_now", lambda: later)

    return advance


PAST_THE_WINDOW = timedelta(days=settings.news_window_days_after + 2)


# ------------------------------------------------------------------ PARTIAL


async def test_enrichment_inside_an_open_window_is_partial(session, stub_llm, recent_move):
    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm)

    movement = await only_movement(session)
    assert movement.news_status == NewsStatus.PARTIAL
    assert movement.news_attempts == 1
    assert movement.news_window_closes_at.replace(tzinfo=timezone.utc) == (
        news_window_closes_at(recent_move)
    )


def test_the_window_closes_grace_hours_after_its_last_day(monkeypatch):
    monkeypatch.setattr(settings, "news_window_days_after", 1)
    monkeypatch.setattr(settings, "news_window_grace_hours", 6)

    closes = news_window_closes_at(date(2026, 7, 2))

    # The last day is 2026-07-03, through 23:59:59.999999 UTC, plus six hours.
    assert closes == datetime(2026, 7, 4, 5, 59, 59, 999999, tzinfo=timezone.utc)


async def test_enrichment_after_the_window_closed_is_complete(session, stub_llm):
    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm)  # 2024 data

    movement = await only_movement(session)
    assert movement.news_status == NewsStatus.COMPLETE


async def test_a_partial_movement_is_not_re_enriched_while_its_window_is_open(
    session, stub_llm, recent_move
):
    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm)
    calls = len(stub_llm.structured_calls)

    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm)

    assert len(stub_llm.structured_calls) == calls
    assert (await only_movement(session)).news_attempts == 1


async def test_a_partial_movement_is_re_enriched_once_its_window_closes(
    session, stub_llm, recent_move, advance_clock
):
    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm)
    advance_clock(PAST_THE_WINDOW)

    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm)

    movement = await only_movement(session)
    assert movement.news_status == NewsStatus.COMPLETE
    assert movement.news_attempts == 2


async def test_re_enrichment_rescores_the_fuller_set_without_duplicates(
    session, stub_llm, recent_move, advance_clock
):
    """The T+1 article arrives on the second pass and outranks the first pass's
    articles. The stub scores candidates by position: [0] easy 0.91, [1] hard
    0.62, the rest irrelevant."""
    provider = ScriptedProvider(published=recent_move)
    provider.easy_urls = ["https://n.test/a", "https://n.test/b"]
    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm, news_provider=provider)

    first = {link.article.url: link.relevance_score for link in await links(session)}
    assert first == {"https://n.test/a": 0.91, "https://n.test/b": 0.62}

    advance_clock(PAST_THE_WINDOW)
    provider.easy_urls = ["https://n.test/late", "https://n.test/a", "https://n.test/b"]
    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm, news_provider=provider)

    second = {link.article.url: link.relevance_score for link in await links(session)}
    # `late` takes the top slot, `a` is re-scored in place, and `b`, which the
    # scorer no longer supports, is unlinked rather than left with a stale score.
    assert second == {"https://n.test/late": 0.91, "https://n.test/a": 0.62}
    assert await session.scalar(sa.select(sa.func.count()).select_from(NewsArticle)) == 3
    assert (await only_movement(session)).news_status == NewsStatus.COMPLETE


async def test_an_empty_second_search_keeps_the_first_pass_links(
    session, stub_llm, recent_move, advance_clock
):
    provider = ScriptedProvider(published=recent_move)
    provider.easy_urls = ["https://n.test/a"]
    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm, news_provider=provider)

    advance_clock(PAST_THE_WINDOW)
    provider.easy_urls = []
    await ingestion.ingest_ticker(session, "TEST", llm=stub_llm, news_provider=provider)

    assert [link.article.url for link in await links(session)] == ["https://n.test/a"]


async def links(session) -> list[MovementNewsLink]:
    session.expire_all()
    return list((await session.scalars(sa.select(MovementNewsLink))).all())


# ------------------------------------------------------------- attempt cap


async def test_a_failed_movement_is_retried_below_the_attempt_cap(session, stub_llm):
    await ingestion.ingest_ticker(
        session, "TEST", llm=stub_llm, news_provider=FailingProvider()
    )
    assert (await only_movement(session)).news_status == NewsStatus.FAILED

    await ingestion.ingest_ticker(
        session, "TEST", llm=stub_llm, news_provider=FixtureNewsProvider()
    )

    movement = await only_movement(session)
    assert movement.news_status == NewsStatus.COMPLETE
    assert movement.news_attempts == 2


async def test_a_movement_past_the_attempt_cap_is_not_retried_automatically(
    session, stub_llm, monkeypatch
):
    monkeypatch.setattr(settings, "news_max_attempts", 2)
    for _ in range(2):
        await ingestion.ingest_ticker(
            session, "TEST", llm=stub_llm, news_provider=FailingProvider()
        )
    assert (await only_movement(session)).news_attempts == 2
    calls = len(stub_llm.structured_calls)

    await ingestion.ingest_ticker(
        session, "TEST", llm=stub_llm, news_provider=FixtureNewsProvider()
    )

    movement = await only_movement(session)
    assert movement.news_status == NewsStatus.FAILED
    assert movement.news_attempts == 2
    assert len(stub_llm.structured_calls) == calls


async def test_an_explicit_retry_overrides_the_attempt_cap(session, stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "news_max_attempts", 1)
    await ingestion.ingest_ticker(
        session, "TEST", llm=stub_llm, news_provider=FailingProvider()
    )

    await ingestion.ingest_ticker(
        session,
        "TEST",
        llm=stub_llm,
        news_provider=FixtureNewsProvider(),
        retry_exhausted=True,
    )

    movement = await only_movement(session)
    assert movement.news_status == NewsStatus.COMPLETE
    assert movement.news_attempts == 2
