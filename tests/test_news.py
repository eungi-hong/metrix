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


# ------------------------------------------------ hard-tier cache sharing


def _context(symbol: str, company: str, sector: str = "Technology"):
    from datetime import date

    from app.services.news.queries import MovementContext

    return MovementContext(
        symbol=symbol,
        company_name=company,
        sector=sector,
        industry="Widgets",
        movement_date=date(2026, 7, 2),
        daily_return=-0.05,
        direction="down",
    )


def test_hard_tier_requests_are_identical_across_companies_in_a_sector():
    """Regression: the Hard request carried a company-specific `summary_query`,
    and the cache key hashes every field, so sector-mates never shared a row."""
    from app.services.news.queries import build_tier_queries
    from app.services.peers import PeerSet

    a = dict(build_tier_queries(_context("AAA", "Alpha Corp"), PeerSet()))
    b = dict(build_tier_queries(_context("BBB", "Beta Inc"), PeerSet()))

    assert a["hard"] == b["hard"]
    assert a["hard"].cache_fingerprint("exa") == b["hard"].cache_fingerprint("exa")
    assert "Alpha" not in (a["hard"].summary_query or "")
    # The company tiers stay company-specific.
    assert a["easy"].cache_fingerprint("exa") != b["easy"].cache_fingerprint("exa")
    assert "Alpha Corp" in (a["easy"].summary_query or "")


def test_a_different_sector_is_a_different_hard_tier_entry():
    from app.services.news.queries import build_tier_queries
    from app.services.peers import PeerSet

    tech = dict(build_tier_queries(_context("AAA", "Alpha"), PeerSet()))["hard"]
    energy = dict(
        build_tier_queries(_context("BBB", "Beta", sector="Energy"), PeerSet())
    )["hard"]
    assert tech.cache_fingerprint("exa") != energy.cache_fingerprint("exa")


async def test_same_sector_tickers_make_one_upstream_hard_tier_call(
    session, stub_llm, monkeypatch
):
    """Two companies, same sector, same movement date: one macro search."""
    import dataclasses

    from app.services import ingestion
    from app.services import prices as price_service
    from tests.conftest import build_price_history

    async def fetch(symbol: str, days: int | None = None):
        history = build_price_history(symbol)
        profile = dataclasses.replace(history.profile, company_name=f"{symbol} Holdings")
        return dataclasses.replace(history, profile=profile)

    monkeypatch.setattr(price_service, "fetch_price_history", fetch)

    calls: list[str] = []

    class CountingProvider(FixtureNewsProvider):
        async def execute(self, req):
            calls.append(req.query)
            return await super().execute(req)

    provider = CachingNewsProvider(CountingProvider(), session)
    await ingestion.ingest_ticker(session, "AAA", llm=stub_llm, news_provider=provider)
    await ingestion.ingest_ticker(session, "BBB", llm=stub_llm, news_provider=provider)

    hard = [q for q in calls if q.startswith("macroeconomic")]
    easy = [q for q in calls if "company news" in q]
    assert len(hard) == 1, "the second ticker's macro search must come from the cache"
    assert len(easy) == 2, "company searches are per ticker"


# ------------------------------------------------------- cache store race


async def test_a_concurrent_insert_of_the_same_key_is_treated_as_a_hit(
    session, monkeypatch
):
    """Two workers miss on the same key and both insert. The loser must not
    raise, and must not lose the rest of its transaction."""
    import sqlalchemy as sa

    from app.models.market import Ticker
    from app.models.news import NewsQueryCache

    cached = CachingNewsProvider(FixtureNewsProvider(), session)
    await cached.search(request())  # the other worker's row
    await session.commit()

    # This worker's own pending work, in the same transaction as the insert.
    session.add(Ticker(symbol="KEEP"))
    await session.flush()

    async def always_miss(key):
        return None

    monkeypatch.setattr(cached, "_lookup", always_miss)
    results = await cached.search(request())  # collides on the unique key

    assert results
    await session.commit()
    assert await session.scalar(sa.select(Ticker).where(Ticker.symbol == "KEEP"))
    assert await session.scalar(sa.select(sa.func.count()).select_from(NewsQueryCache)) == 1


# ---------------------------------------------------- window-aware cache TTL


def test_a_closed_window_is_cached_for_the_long_ttl(monkeypatch):
    from app.core.config import settings
    from app.services.news.cache import cache_expiry

    monkeypatch.setattr(settings, "news_cache_closed_window_ttl_days", 90)
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    closed = NewsSearchRequest(query="q", start=now - timedelta(days=10), end=now - timedelta(days=5))

    assert cache_expiry(closed, now) == now + timedelta(days=90)


def test_an_open_window_is_cached_briefly(monkeypatch):
    from app.core.config import settings
    from app.services.news.cache import cache_expiry

    monkeypatch.setattr(settings, "news_cache_open_window_ttl_hours", 2)
    monkeypatch.setattr(settings, "news_window_grace_hours", 6)
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    open_ = NewsSearchRequest(query="q", start=now - timedelta(days=3), end=now + timedelta(days=1))

    assert cache_expiry(open_, now) == now + timedelta(hours=2)


def test_an_open_window_entry_never_outlives_the_window(monkeypatch):
    """Cached an hour before closing, the entry must expire at closing, so the
    first search after it sees the final answer rather than a provisional one."""
    from app.core.config import settings
    from app.services.news.cache import cache_expiry

    monkeypatch.setattr(settings, "news_cache_open_window_ttl_hours", 2)
    monkeypatch.setattr(settings, "news_window_grace_hours", 6)
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    end = now - timedelta(hours=5)  # closes at now + 1h
    closing = NewsSearchRequest(query="q", start=end - timedelta(days=4), end=end)

    assert cache_expiry(closing, now) == now + timedelta(hours=1)


async def test_the_stored_entry_uses_the_window_aware_expiry(session):
    import sqlalchemy as sa

    from app.models.news import NewsQueryCache

    cached = CachingNewsProvider(FixtureNewsProvider(), session)
    await cached.search(request())  # a 2026 window: closed

    row = await session.scalar(sa.select(NewsQueryCache))
    expires = row.expires_at.replace(tzinfo=timezone.utc)
    assert expires - datetime.now(timezone.utc) > timedelta(days=30)
