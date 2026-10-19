"""Ask Metrix's tools: read-only, labelled rows, errors the model can read."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.models.identity import Plan
from app.models.filings import Filing, MovementFiling
from app.models.jobs import Job
from app.models.market import Movement
from app.models.news import NewsArticle
from app.services import chat_tools, ingestion, sessions
from app.services.auth import Principal
from app.services.llm.base import ToolCall
from tests.conftest import POSTGRES, postgres_session_factory
from tests.test_finance_api import ready  # noqa: F401  (fixture)

ME = Principal(plan=Plan.PRO, client_ip="1.2.3.4", user_id=1)


async def call(session, name: str, **arguments):
    turn = chat_tools.Turn(session=session, principal=ME)
    result = await chat_tools.run(turn, ToolCall(id="c1", name=name, arguments=arguments))
    return turn, json.loads(result.content), result


async def test_movements_are_labelled_by_id_with_their_top_evidence(session_factory, ready):  # noqa: F811
    _, company_day, _ = ready
    async with session_factory() as session:
        turn, body, result = await call(session, "get_movements", symbol="synth", sort="abs_return", limit=3)
        company = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
    first = body["movements"][0]
    assert first["label"] == f"M{company.id}" and first["date"] == company_day.isoformat()
    assert first["dominant"] == "company" and first["evidence"] == "explained"
    assert first["top_evidence"]["title"] == "https://n.test/current"  # the superseded link is not shown
    assert set(turn.movements) == {row["label"] for row in body["movements"]}
    assert first["top_evidence"]["label"] in turn.articles
    assert body["matching_movements"] >= 3 and result.rows == 3


async def test_filters_and_bad_input(session_factory, ready):  # noqa: F811
    async with session_factory() as session:
        _, downs, _ = await call(session, "get_movements", symbol="SYNTH", direction="down", min_abs_return_pct=5)
        assert all(m["return_pct"] <= -5 for m in downs["movements"])
        for name, args, message in (
            ("get_movements", {"symbol": "ZZZZ"}, "no data for ZZZZ"),
            ("get_movements", {"symbol": "SYNTH", "start": "June"}, "YYYY-MM-DD"),
            ("get_movement_case", {"symbol": "SYNTH", "date": "1999-01-04"}, "no flagged movement"),
            ("nope", {}, "unknown tool"),
        ):
            _, body, result = await call(session, name, **args)
            assert message in body["error"].lower() or message in body["error"], body
            assert result.rows == 0
        bad = await chat_tools.run(chat_tools.Turn(session=session, principal=ME),
                                   ToolCall("c", "compare", {}, error="invalid arguments: x"))
        assert json.loads(bad.content) == {"error": "invalid arguments: x"}


async def test_the_case_has_articles_filings_peers_and_decomposition(session_factory, ready):  # noqa: F811
    data, company_day, _ = ready
    frame = sessions.frame_for(company_day)
    async with session_factory() as session:
        company = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
        filing = Filing(ticker_id=company.ticker_id, cik=1, accession="0001-26-1", form="8-K",
                        filing_date=frame.previous, accepted_at=frame.prior_close + timedelta(minutes=5), items=["2.02"])
        session.add(filing)
        await session.flush()
        session.add(MovementFiling(movement_id=company.id, filing_id=filing.id, timing="overnight", timing_precision="time"))
        synth = await ingestion.get_ticker(session, "SYNTH")
        synth.peers = {"peers": [{"name": "Sector Fund", "ticker": "XLK", "relationship": "competitor"}]}
        await session.commit()

        turn, case, _ = await call(session, "get_movement_case", symbol="SYNTH", date=company_day.isoformat())
    assert case["decomposition"]["company_pct"] == pytest.approx(case["abnormal_return_pct"])
    assert [a["title"] for a in case["articles"]] == ["https://n.test/current"]
    assert case["filings"][0]["label"] == f"F{filing.id}" and case["filings"][0]["events"] == ["results of operations (earnings)"]
    assert case["peers"][0]["return_pct"] == case["benchmarks"]["XLK"]
    assert {f"F{filing.id}", case["label"]} <= turn.labels


async def test_search_matches_linked_articles_only(session_factory, ready):  # noqa: F811
    _, company_day, _ = ready
    async with session_factory() as session:
        article = await session.scalar(sa.select(NewsArticle).where(NewsArticle.url == "https://n.test/current"))
        article.title = "Synth cuts full-year guidance on export restrictions"
        session.add(NewsArticle(url_hash="x", url="https://n.test/unlinked", title="Export restrictions everywhere",
                                provider="test"))
        await session.commit()
        turn, body, _ = await call(session, "search_evidence", query="export restrictions", symbol="SYNTH")
    (hit,) = body["results"]
    assert hit["title"].startswith("Synth cuts") and hit["movement"]["date"] == company_day.isoformat()
    assert hit["label"] in turn.articles and hit["movement"]["label"] in turn.movements


async def test_filings_and_compare(session_factory, ready):  # noqa: F811
    data, _, _ = ready
    async with session_factory() as session:
        ticker = await ingestion.get_ticker(session, "SYNTH")
        session.add(Filing(ticker_id=ticker.id, cik=1, accession="0001-26-2", form="10-Q",
                           filing_date=data.dates[-5], items=[]))
        await session.commit()
        _, filings, _ = await call(session, "get_filings", symbol="SYNTH", form="10-q")
        _, none, _ = await call(session, "get_filings", symbol="SYNTH", items=["5.02"])
        _, compared, _ = await call(session, "compare", symbols=["SYNTH", "XLK"],
                                    start=data.dates[-21].isoformat(), end=data.dates[-1].isoformat())
    assert [f["form"] for f in filings["filings"]] == ["10-Q"] and none["filings"] == []
    synth = next(t for t in compared["tickers"] if t["symbol"] == "SYNTH")
    expected = data.stock.bars[-1].adj_close / data.stock.bars[-21].adj_close - 1
    assert synth["return_pct"] == pytest.approx(expected * 100, abs=0.01)
    spy = data.market.bars[-1].adj_close / data.market.bars[-21].adj_close - 1
    assert synth["vs_market_pct"] == pytest.approx((expected - spy) * 100, abs=0.02)


async def test_the_watchlist_tool_sees_only_the_callers_lists(session_factory, ready):  # noqa: F811
    from app.models.identity import User
    from app.models.watchlists import Watchlist, WatchlistHolding

    async with session_factory() as session:
        session.add(User(id=1, name="Me", plan=Plan.PRO))
        await session.flush()
        mine = Watchlist(name="Mine", user_id=1, holdings=[WatchlistHolding(symbol="SYNTH", position=0)])
        theirs = Watchlist(name="Theirs", anonymous_key="anon:9.9.9.9", holdings=[])
        session.add_all([mine, theirs])
        await session.commit()
        turn, body, _ = await call(session, "get_watchlist_digest", id=mine.id, sessions=60)
        _, refused, _ = await call(session, "get_watchlist_digest", id=theirs.id)
    assert body["name"] == "Mine" and body["holdings"][0]["moves"]
    assert all(mv["label"] in turn.movements for mv in body["holdings"][0]["moves"])
    assert "no such watchlist" in refused["error"]


async def test_tools_never_queue_work_or_write(session_factory, ready):  # noqa: F811
    async with session_factory() as session:
        before = await session.scalar(sa.select(sa.func.max(Movement.updated_at)))
        for name, args in (("get_movements", {"symbol": "SYNTH"}), ("search_evidence", {"query": "x"}),
                           ("get_filings", {"symbol": "SYNTH"}), ("compare", {"symbols": ["SYNTH"]})):
            await call(session, name, **args)
        assert not session.dirty and not session.new
        assert await session.scalar(sa.select(sa.func.count()).select_from(Job)) == 0
        assert await session.scalar(sa.select(sa.func.max(Movement.updated_at))) == before


def test_tool_definitions_are_stable_and_strict():
    names = [t.name for t in chat_tools.TOOLS]
    assert names == ["get_movements", "get_movement_case", "search_evidence", "get_filings", "compare",
                     "get_watchlist_digest"]
    for tool in chat_tools.TOOLS:
        assert tool.parameters["type"] == "object" and tool.parameters["additionalProperties"] is False
        assert "datetime" not in json.dumps(tool.parameters)  # nothing dynamic in the cached prefix


async def test_large_results_are_capped(session_factory, ready, monkeypatch):  # noqa: F811
    monkeypatch.setattr(chat_tools, "MAX_RESULT_CHARS", 300)
    async with session_factory() as session:
        turn = chat_tools.Turn(session=session, principal=ME)
        result = await chat_tools.run(turn, ToolCall("c", "get_movements", {"symbol": "SYNTH", "limit": 20}))
    assert len(result.content) <= 300 and result.content.endswith('"(truncated)"')




@pytest.mark.parametrize("with_column", [pytest.param(False, marks=POSTGRES), pytest.param(True, marks=POSTGRES)])
async def test_postgres_full_text_search(with_column):
    """websearch_to_tsquery over title, summary and content: inline, or via
    the migration's generated, indexed column."""
    from datetime import date

    from app.models.enums import Direction, NewsStatus, RelevanceTier
    from app.models.market import Ticker
    from app.models.news import MovementNewsLink, url_fingerprint

    chat_tools._search_column.clear()
    async with postgres_session_factory() as factory:
        async with factory() as session:
            if with_column:
                await session.execute(sa.text(
                    "ALTER TABLE news_articles ADD COLUMN search_vector tsvector GENERATED ALWAYS AS ("
                    "setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') || "
                    "setweight(to_tsvector('english'::regconfig, coalesce(summary, '')), 'B') || "
                    "setweight(to_tsvector('english'::regconfig, left(coalesce(content, ''), 20000)), 'C')) STORED"))
            ticker = Ticker(symbol="FTS", last_ingested_at=datetime.now(timezone.utc))
            session.add(ticker)
            await session.flush()
            m = Movement(ticker_id=ticker.id, date=date(2026, 6, 3), daily_return=-0.07, abs_return=0.07,
                         direction=Direction.DOWN, prev_adj_close=100, adj_close=93, threshold=0.03,
                         threshold_source="floor", detector_k=2.0, detector_window=20, detector_floor=0.03,
                         news_status=NewsStatus.COMPLETE, news_window_closes_at=datetime(2026, 6, 6, tzinfo=timezone.utc))
            session.add(m)
            await session.flush()
            for i, (title, summary) in enumerate((("Chipmaker slides", "New export restrictions on advanced chips"),
                                                  ("Unrelated", "Quarterly dividend declared"))):
                a = NewsArticle(url_hash=url_fingerprint(f"https://n.test/{i}"), url=f"https://n.test/{i}", title=title,
                                summary=summary, provider="test")
                session.add(a)
                await session.flush()
                session.add(MovementNewsLink(movement_id=m.id, article_id=a.id, relevance_tier=RelevanceTier.HARD,
                                             relevance_score=0.8 - i / 10))
            await session.commit()
            _, body, _ = await call(session, "search_evidence", query='"export restriction" -dividend')
            assert await chat_tools._has_search_column(session) is with_column
    assert [r["title"] for r in body["results"]] == ["Chipmaker slides"]  # stemmed, phrase, negation
