"""The read path's data version, ETag validation and shared response cache."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.api.routes import tickers as ticker_routes
from app.models.market import Movement, Ticker
from app.models.news import NewsArticle
from app.services import ingestion, response_cache
from tests.test_finance_api import add_link, ready  # noqa: F401  (fixture)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttl: dict[str, int] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: bytes, ex: int | None = None) -> None:
        self.store[key] = value
        self.ttl[key] = ex


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(response_cache, "get_redis", lambda: fake)
    return fake


async def version(session_factory, symbol: str = "SYNTH") -> int:
    async with session_factory() as session:
        return await session.scalar(sa.select(Ticker.data_version).where(Ticker.symbol == symbol))


# -- the data version --------------------------------------------------------


async def test_committed_writes_bump_the_version_and_rollbacks_do_not(session_factory, ready):  # noqa: F811
    _, company_day, _ = ready
    before = await version(session_factory)

    async with session_factory() as session:
        movement = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
        movement.abstain_reason = "edited"
        await session.rollback()
    assert await version(session_factory) == before

    async with session_factory() as session:
        movement = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
        movement.abstain_reason = "edited"
        await session.commit()
    after_edit = await version(session_factory)
    assert after_edit == before + 1

    async with session_factory() as session:
        movement = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
        await add_link(session, movement, "https://n.test/new")
        await session.commit()
    after_link = await version(session_factory)
    assert after_link == after_edit + 1

    async with session_factory() as session:
        article = await session.scalar(sa.select(NewsArticle).where(NewsArticle.url == "https://n.test/new"))
        article.title = "Retitled"
        await session.commit()
    assert await version(session_factory) == after_link + 1


async def test_an_unrelated_ticker_and_reads_do_not_bump(session_factory, ready):  # noqa: F811
    before = await version(session_factory)
    async with session_factory() as session:
        other = await ingestion.get_or_create_ticker(session, "OTHR")
        await session.commit()
        await session.scalar(sa.select(Movement).limit(1))
        await session.commit()
        assert other.id is not None
    assert await version(session_factory) == before


async def test_bulk_supersede_and_claim_bump_too(session_factory, ready):  # noqa: F811
    _, company_day, _ = ready
    before = await version(session_factory)
    async with session_factory() as session:
        movement = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
        await ingestion._supersede_links(session, movement, set())
        await session.commit()
    assert await version(session_factory) == before + 1

    async with session_factory() as session:
        ticker = await ingestion.get_ticker(session, "SYNTH")
        assert await ingestion.claim_ingestion(session, ticker)
    assert await version(session_factory) == before + 2


async def test_a_released_savepoint_does_not_bump_before_the_commit(session_factory, ready):  # noqa: F811
    _, company_day, _ = ready
    before = await version(session_factory)
    async with session_factory() as session:
        movement = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
        async with session.begin_nested():
            movement.abstain_reason = "inside a savepoint"
        # Read in the same transaction, which would see its own bump.
        assert await session.scalar(sa.select(Ticker.data_version).where(Ticker.symbol == "SYNTH")) == before
        await session.commit()
    assert await version(session_factory) == before + 1


# -- ETag and 304 ------------------------------------------------------------


async def test_a_settled_response_has_an_etag_and_revalidates_to_304(client, ready):  # noqa: F811
    first = await client.get("/tickers/SYNTH")
    etag = first.headers["ETag"]
    assert first.status_code == 200 and etag.startswith('"') and len(etag) == 34
    assert first.headers["Cache-Control"] == "private, max-age=60, stale-while-revalidate=300"
    assert first.headers["Vary"] == "Authorization"

    again = await client.get("/tickers/SYNTH", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["ETag"] == etag
    listed = await client.get("/tickers/SYNTH", headers={"If-None-Match": f'"other", W/{etag}'})
    assert listed.status_code == 304


async def test_the_etag_follows_the_data_and_the_parameters(client, ready, session_factory):  # noqa: F811
    _, company_day, _ = ready
    etag = (await client.get("/tickers/SYNTH")).headers["ETag"]
    assert (await client.get("/tickers/SYNTH")).headers["ETag"] == etag  # stable
    assert (await client.get("/tickers/SYNTH?sort=abs_sar")).headers["ETag"] != etag
    assert (await client.get("/tickers/SYNTH?include_benchmarks=true")).headers["ETag"] != etag

    async with session_factory() as session:
        movement = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
        await add_link(session, movement, "https://n.test/later")
        await session.commit()

    changed = await client.get("/tickers/SYNTH", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["ETag"] != etag
    move = next(m for m in changed.json()["movements"] if m["date"] == company_day.isoformat())
    assert "https://n.test/later" in {a["article"]["url"] for a in move["news"]}


async def test_a_benchmark_update_changes_the_tag_only_when_benchmarks_are_shown(client, ready, session_factory):  # noqa: F811
    plain = (await client.get("/tickers/SYNTH")).headers["ETag"]
    overlay = (await client.get("/tickers/SYNTH?include_benchmarks=true")).headers["ETag"]
    async with session_factory() as session:
        spy = await ingestion.get_ticker(session, "SPY")
        spy.company_name = "SPDR S&P 500"
        await session.commit()
    assert (await client.get("/tickers/SYNTH")).headers["ETag"] == plain
    assert (await client.get("/tickers/SYNTH?include_benchmarks=true")).headers["ETag"] != overlay


async def test_unsettled_responses_are_not_cached(client):
    response = await client.get("/tickers/AAPL")  # cold: ingestion queued
    assert response.status_code == 202
    assert "ETag" not in response.headers


# -- the shared cache --------------------------------------------------------


async def test_a_second_request_is_served_from_the_cache(client, ready, redis, monkeypatch):  # noqa: F811
    first = await client.get("/tickers/SYNTH?sort=abs_return")
    assert len(redis.store) == 1 and list(redis.ttl.values()) == [300]

    async def must_not_build(*args, **kwargs):
        raise AssertionError("the response was rebuilt")

    monkeypatch.setattr(ticker_routes, "_price_range", must_not_build)
    second = await client.get("/tickers/SYNTH?sort=abs_return")

    assert second.status_code == 200 and second.content == first.content
    assert second.headers["ETag"] == first.headers["ETag"]


async def test_a_changed_ticker_misses_the_old_entry(client, ready, redis, session_factory):  # noqa: F811
    _, company_day, _ = ready
    await client.get("/tickers/SYNTH")
    async with session_factory() as session:
        movement = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
        movement.abstain_reason = "changed"
        await session.commit()
    await client.get("/tickers/SYNTH")
    assert len(redis.store) == 2  # a new key; the old one is never read again


async def test_without_redis_everything_still_works(client, ready, monkeypatch):  # noqa: F811
    monkeypatch.setattr(response_cache, "get_redis", lambda: None)
    assert (await client.get("/tickers/SYNTH")).status_code == 200


async def test_a_failing_redis_is_a_miss(client, ready, monkeypatch):  # noqa: F811
    class Broken:
        async def get(self, key):
            raise ConnectionError("down")

        async def set(self, *a, **kw):
            raise ConnectionError("down")

    monkeypatch.setattr(response_cache, "get_redis", lambda: Broken())
    assert (await client.get("/tickers/SYNTH")).status_code == 200


def test_if_none_match_parsing():
    tag = '"abc"'
    assert response_cache.matches('"abc"', tag)
    assert response_cache.matches('W/"abc"', tag)
    assert response_cache.matches('"x", "abc"', tag)
    assert response_cache.matches("*", tag)
    assert not response_cache.matches('"abd"', tag)
    assert not response_cache.matches(None, tag)
