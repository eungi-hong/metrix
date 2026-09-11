"""Tests for the news layer: window enforcement, deduplication, and caching."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.news import normalize_url, url_fingerprint
from app.services.news.base import NewsCandidate, NewsProvider, NewsSearchRequest
from app.services.news.cache import CachingNewsProvider
from app.services.news.fixture import FixtureNewsProvider
from app.services.relevance import deduplicate

START = datetime(2026, 6, 29, tzinfo=timezone.utc)
END = datetime(2026, 7, 3, 23, 59, 59, tzinfo=timezone.utc)


def request(num_results: int = 4) -> NewsSearchRequest:
    return NewsSearchRequest(query="apple earnings", start=START, end=END, num_results=num_results)


def candidate(url: str, published: datetime | None) -> NewsCandidate:
    return NewsCandidate(url=url, title="t", published_at=published, provider="test")


# ----------------------------------------------------------- window enforcement


def test_articles_published_after_the_window_are_dropped():
    """Regression: Exa returned a 2026-07-30 article for a 2026-07-02 movement,
    and the scorer rated it 0.95 as the cause of a move four weeks earlier."""
    late = candidate("https://x.test/a", datetime(2026, 7, 30, tzinfo=timezone.utc))
    kept = NewsProvider.enforce_window(request(), [late])
    assert kept == []


def test_articles_published_before_the_window_are_dropped():
    early = candidate("https://x.test/b", datetime(2026, 5, 1, tzinfo=timezone.utc))
    assert NewsProvider.enforce_window(request(), [early]) == []


@pytest.mark.parametrize(
    "published",
    [START, END, datetime(2026, 7, 1, 12, tzinfo=timezone.utc)],
)
def test_articles_inside_the_window_including_its_bounds_are_kept(published):
    inside = candidate("https://x.test/c", published)
    assert NewsProvider.enforce_window(request(), [inside]) == [inside]


def test_articles_with_no_publication_date_are_kept():
    """Dropping these loses too much; the scorer is told the date is unknown."""
    undated = candidate("https://x.test/d", None)
    assert NewsProvider.enforce_window(request(), [undated]) == [undated]


def test_naive_publication_dates_are_treated_as_utc():
    naive = candidate("https://x.test/e", datetime(2026, 7, 1, 12))
    assert NewsProvider.enforce_window(request(), [naive]) == [naive]


async def test_search_applies_the_window(monkeypatch):
    provider = FixtureNewsProvider()

    async def leaky(_request):
        return {
            "results": [
                {"url": "https://x.test/in", "title": "in", "publishedDate": "2026-07-01T00:00:00Z"},
                {"url": "https://x.test/out", "title": "out", "publishedDate": "2026-07-30T00:00:00Z"},
            ]
        }

    monkeypatch.setattr(provider, "execute", leaky)
    results = await provider.search(request())
    assert [c.url for c in results] == ["https://x.test/in"]


# --------------------------------------------------------------- deduplication


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://www.reuters.com/tech/story", "https://reuters.com/tech/story"),
        ("http://reuters.com/tech/story", "https://reuters.com/tech/story"),
        ("https://reuters.com/tech/story/", "https://reuters.com/tech/story"),
        ("https://reuters.com/tech/story?utm_source=x", "https://reuters.com/tech/story"),
        ("https://REUTERS.com/tech/story", "https://reuters.com/tech/story"),
    ],
)
def test_urls_that_differ_only_cosmetically_share_a_fingerprint(a, b):
    assert url_fingerprint(a) == url_fingerprint(b)
    assert normalize_url(a) == normalize_url(b)


def test_genuinely_different_urls_do_not_collide():
    assert url_fingerprint("https://r.test/a") != url_fingerprint("https://r.test/b")


def test_meaningful_query_parameters_are_preserved():
    assert normalize_url("https://r.test/a?id=7") != normalize_url("https://r.test/a")


def test_deduplicate_keeps_the_first_tier_that_found_an_article():
    shared = candidate("https://www.r.test/story", None)
    duplicate = candidate("https://r.test/story/", None)

    unique = deduplicate([("easy", shared), ("hard", duplicate)])

    assert len(unique) == 1
    assert unique[0][0] == "easy"


# --------------------------------------------------------------------- caching


async def test_repeated_searches_hit_the_cache_instead_of_the_provider(session):
    calls: list[str] = []

    class CountingProvider(FixtureNewsProvider):
        async def execute(self, req):
            calls.append(req.query)
            return await super().execute(req)

    cached = CachingNewsProvider(CountingProvider(), session)

    first = await cached.search(request())
    second = await cached.search(request())

    assert len(calls) == 1, "the second identical search must be served from Postgres"
    assert [c.url for c in first] == [c.url for c in second]


async def test_a_different_window_is_a_different_cache_entry(session):
    calls: list[str] = []

    class CountingProvider(FixtureNewsProvider):
        async def execute(self, req):
            calls.append(req.query)
            return await super().execute(req)

    cached = CachingNewsProvider(CountingProvider(), session)

    await cached.search(request())
    await cached.search(
        NewsSearchRequest(query="apple earnings", start=START, end=END + timedelta(days=1))
    )

    assert len(calls) == 2


def test_the_cache_key_separates_providers_and_queries():
    req = request()
    assert req.cache_fingerprint("exa") != req.cache_fingerprint("fixture")
    assert req.cache_fingerprint("exa") != request(num_results=9).cache_fingerprint("exa")


# ------------------------------------ window enforcement against stored rows


async def test_a_deduplicated_article_is_not_linked_outside_its_window(
    session_factory, stub_llm, monkeypatch
):
    """Regression: a search returned an already-stored article with no date, so
    the provider filter had nothing to test. Deduplication resolved it to a row
    published 28 days after the movement, which then got linked and scored 0.95.
    """
    import sqlalchemy as sa

    from app.models.market import Movement
    from app.models.news import MovementNewsLink, NewsArticle
    from app.services import ingestion
    from app.services.news.fixture import FixtureNewsProvider

    out_of_window_url = "https://x.test/quarterly-results"

    class LeakyProvider(FixtureNewsProvider):
        """Returns the article with its publication date missing."""

        async def execute(self, req):
            return {
                "results": [
                    {
                        "url": out_of_window_url,
                        "title": "Quarterly results",
                        "publishedDate": None,
                    }
                ]
            }

    async with session_factory() as session:
        # The article is already on file, published far outside any window.
        session.add(
            NewsArticle(
                url_hash=url_fingerprint(out_of_window_url),
                url=out_of_window_url,
                title="Quarterly results",
                published_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                provider="test",
            )
        )
        await session.commit()

        await ingestion.ingest_ticker(
            session, "TEST", llm=stub_llm, news_provider=LeakyProvider()
        )

        links = (await session.scalars(sa.select(MovementNewsLink))).all()
        assert links == [], "an article published outside the window must not be linked"
        assert (await session.scalars(sa.select(Movement))).all(), "movements still detected"
