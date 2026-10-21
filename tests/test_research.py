"""The read-only research endpoints: symbol search, the case file, filings,
earnings and the market pulse."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.models.filings import Filing, MovementFiling
from app.models.market import Movement, Ticker
from app.models.symbols import ListedSymbol
from app.services import ingestion, sessions
from tests.test_finance_api import ready  # noqa: F401  (fixture)

NOW = datetime(2026, 3, 4, tzinfo=timezone.utc)


# -- sessions helper ----------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2024, 5, 22, 20, 20, tzinfo=timezone.utc), (date(2024, 5, 23), "after_close")),  # 16:20 NY Wed
        (datetime(2024, 5, 23, 11, 0, tzinfo=timezone.utc), (date(2024, 5, 23), "before_open")),  # 07:00 NY
        (datetime(2024, 5, 23, 15, 0, tzinfo=timezone.utc), (date(2024, 5, 23), "during_session")),
        (datetime(2024, 5, 24, 21, 0, tzinfo=timezone.utc), (date(2024, 5, 28), "after_close")),  # Fri, then Memorial Day
    ],
)
def test_first_affected_session(at, expected):
    assert sessions.first_affected_session(at) == expected


def test_a_date_only_release_is_placed_on_that_session_or_the_next():
    assert sessions.first_affected_session(None, date(2024, 5, 23)) == (date(2024, 5, 23), "unknown")
    assert sessions.first_affected_session(None, date(2024, 5, 25)) == (date(2024, 5, 28), "unknown")
    assert sessions.first_affected_session(None, None) == (None, "unknown")


# -- symbol search ------------------------------------------------------------


async def test_search_ranks_exact_then_prefix_then_name(client, session_factory, ready):  # noqa: F811
    async with session_factory() as session:
        for symbol, name in (("NVDA", "NVIDIA Corporation"), ("NVD", "Some Leveraged NVIDIA ETF"),
                             ("NVDL", "GraniteShares 2x Long NVDA"), ("ANVS", "Annovis Bio"),
                             ("NVO", "Novo Nordisk")):
            session.add(ListedSymbol(symbol=symbol, name=name, is_etf=symbol != "NVDA", source="nasdaqlisted",
                                     refreshed_at=NOW))
        await session.commit()

    body = (await client.get("/symbols/search", params={"q": "nvd"})).json()
    assert [r["symbol"] for r in body["results"]] == ["NVD", "NVDA", "NVDL"]

    by_name = (await client.get("/symbols/search", params={"q": "nvidia"})).json()
    assert [r["symbol"] for r in by_name["results"]][:1] == ["NVDA"]  # name starts with it
    assert "NVD" in [r["symbol"] for r in by_name["results"]]  # contains it

    held = (await client.get("/symbols/search", params={"q": "SYN"})).json()  # not in the directory, but held
    assert held["results"][0]["symbol"] == "SYNTH" and held["results"][0]["has_data"] is True


async def test_search_treats_wildcards_literally(client, session_factory):
    async with session_factory() as session:
        session.add(ListedSymbol(symbol="ABC", name="Alpha Beta", is_etf=False, source="x", refreshed_at=NOW))
        await session.commit()
    assert (await client.get("/symbols/search", params={"q": "%"})).json()["results"] == []
    assert (await client.get("/symbols/search", params={"q": "_"})).json()["results"] == []
    assert (await client.get("/symbols/search", params={"q": ""})).status_code == 422


# -- case file ----------------------------------------------------------------


async def add_peer(session_factory, data) -> None:
    async with session_factory() as session:
        peer = await ingestion.get_or_create_ticker(session, "PEER")
        await ingestion.store_benchmark_prices(session, peer, data.sector, benchmark=False)
        synth = await ingestion.get_ticker(session, "SYNTH")
        synth.peers = {"peers": [
            {"name": "Peer Co", "ticker": "PEER", "relationship": "competitor"},
            {"name": "Private Co", "ticker": None, "relationship": "supplier"},
        ], "industry_themes": []}
        await session.commit()


async def test_the_case_file_has_everything_about_one_move(client, ready, session_factory):  # noqa: F811
    data, company_day, _ = ready
    await add_peer(session_factory, data)
    response = await client.get(f"/tickers/SYNTH/movements/{company_day.isoformat()}")
    assert response.status_code == 200
    case = response.json()

    assert case["movement"]["date"] == company_day.isoformat()
    frame = sessions.frame_for(company_day)
    assert case["session"]["open"].startswith(frame.open.strftime("%Y-%m-%dT%H:%M"))
    assert case["session"]["previous"] == frame.previous.isoformat()
    assert case["movement"]["evidence"]["state"] == "explained"
    assert case["movement"]["decomposition"]["dominant"] == "company"
    audit = case["detection"]
    assert audit["trigger"] == "both" and audit["raw"]["passed"] is True and audit["abnormal"]["passed"] is True
    assert audit["raw"]["threshold"] == pytest.approx(case["movement"]["threshold"])

    index = data.dates.index(company_day)
    expected_sector = data.sector.bars[index].adj_close / data.sector.bars[index - 1].adj_close - 1
    roles = {b["role"]: b for b in case["benchmarks"]}
    assert roles["sector"]["symbol"] == "XLK"
    assert roles["sector"]["daily_return"] == pytest.approx(expected_sector, abs=1e-6)  # stored prices are rounded
    peers = {p["name"]: p for p in case["peers"]}
    assert peers["Peer Co"]["daily_return"] == pytest.approx(expected_sector, abs=1e-6)
    assert peers["Private Co"]["symbol"] is None and peers["Private Co"]["daily_return"] is None
    assert case["previous_date"] < company_day.isoformat()


async def test_the_case_file_revalidates_and_404s(client, ready, session_factory):  # noqa: F811
    _, company_day, _ = ready
    url = f"/tickers/SYNTH/movements/{company_day.isoformat()}"
    first = await client.get(url)
    assert (await client.get(url, headers={"If-None-Match": first.headers["ETag"]})).status_code == 304
    async with session_factory() as session:
        m = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
        m.abstain_reason = "x"
        await session.commit()
    assert (await client.get(url, headers={"If-None-Match": first.headers["ETag"]})).status_code == 200

    assert (await client.get("/tickers/SYNTH/movements/1999-01-04")).status_code == 404
    assert (await client.get(f"/tickers/ZZZZ/movements/{company_day.isoformat()}")).status_code == 404


async def test_research_endpoints_never_queue_work(client, ready, session_factory):  # noqa: F811
    from app.models.jobs import Job

    _, company_day, _ = ready
    await client.get(f"/tickers/AAPL/movements/{company_day.isoformat()}")
    await client.get("/tickers/AAPL/filings")
    await client.get("/market/pulse")
    async with session_factory() as session:
        assert await session.scalar(sa.select(sa.func.count()).select_from(Job)) == 0


# -- filings and earnings ------------------------------------------------------


async def add_filings(session_factory, company_day: date) -> None:
    frame = sessions.frame_for(company_day)
    async with session_factory() as session:
        ticker = await ingestion.get_ticker(session, "SYNTH")
        movement = await session.scalar(sa.select(Movement).where(Movement.date == company_day))
        earnings = Filing(ticker_id=ticker.id, cik=1, accession="0001-26-000001", form="8-K",
                          filing_date=frame.previous, accepted_at=frame.prior_close + timedelta(minutes=20),
                          items=["2.02", "9.01"], url="https://www.sec.gov/x")
        officer = Filing(ticker_id=ticker.id, cik=1, accession="0001-26-000002", form="8-K",
                         filing_date=company_day - timedelta(days=40), accepted_at=None, items=["5.02"])
        quarterly = Filing(ticker_id=ticker.id, cik=1, accession="0001-26-000003", form="10-Q",
                           filing_date=company_day - timedelta(days=60), accepted_at=None, items=[])
        session.add_all([earnings, officer, quarterly])
        await session.flush()
        session.add(MovementFiling(movement_id=movement.id, filing_id=earnings.id, timing="overnight",
                                   timing_precision="time"))
        await session.commit()


async def test_filings_are_listed_filtered_and_tied_to_movements(client, ready, session_factory):  # noqa: F811
    _, company_day, _ = ready
    await add_filings(session_factory, company_day)
    body = (await client.get("/tickers/SYNTH/filings")).json()
    assert [f["accession"] for f in body["filings"]] == ["0001-26-000001", "0001-26-000002", "0001-26-000003"]
    assert body["filings"][0]["movement_dates"] == [company_day.isoformat()]
    assert body["filings"][0]["events"] == ["results of operations (earnings)"]
    assert "not enabled" in body["note"]
    only_8k = (await client.get("/tickers/SYNTH/filings", params={"form": "8-k", "item": "5.02"})).json()
    assert [f["accession"] for f in only_8k["filings"]] == ["0001-26-000002"]


async def test_earnings_are_placed_on_the_session_they_moved(client, ready, session_factory):  # noqa: F811
    _, company_day, _ = ready
    await add_filings(session_factory, company_day)
    body = (await client.get("/tickers/SYNTH/earnings")).json()
    (event,) = body["events"]
    assert (event["session"], event["timing"]) == (company_day.isoformat(), "after_close")
    assert event["movement"]["date"] == company_day.isoformat()
    assert body["source"] == "sec_8k_item_2.02"


# -- pulse --------------------------------------------------------------------


async def test_the_pulse_ranks_abnormal_moves_and_reports_sectors(client, ready, session_factory):  # noqa: F811
    data, company_day, _ = ready
    async with session_factory() as session:
        # Make the company day the latest session the pulse looks at.
        spy = await ingestion.get_ticker(session, "SPY")
        from app.models.market import PriceBar

        await session.execute(sa.delete(PriceBar).where(PriceBar.ticker_id == spy.id, PriceBar.date > company_day))
        await session.commit()
    body = (await client.get("/market/pulse", params={"sessions": 3})).json()
    assert body["sessions"][-1] == company_day.isoformat() and len(body["sessions"]) == 3
    top = body["movers"][0]
    assert top["symbol"] == "SYNTH" and top["date"] == company_day.isoformat()
    assert top["explanation"].startswith("https://n.test/current") and top["evidence"] == "explained"
    sectors = {s["symbol"]: s for s in body["sectors"]}
    assert sectors["SPY"]["sector"] == "Market" and sectors["SPY"]["total_return"] is not None
    assert sectors["XLV"]["total_return"] is None  # no bars stored
    assert all(m["symbol"] not in ("SPY", "XLK") for m in body["movers"])
    featured = body["featured"]
    assert featured is not None and featured["evidence"] == "explained" and featured["symbol"] == "SYNTH"


async def test_the_featured_move_needs_strong_evidence(client, ready, monkeypatch):  # noqa: F811
    from app.core.config import settings

    monkeypatch.setattr(settings, "featured_min_score", 1.0)
    assert (await client.get("/market/pulse")).json()["featured"] is None


async def test_an_empty_pulse(client):
    assert (await client.get("/market/pulse")).json() == {"sessions": [], "movers": [], "featured": None, "sectors": []}


async def test_unknown_tickers_and_bad_symbols(client, session_factory):
    async with session_factory() as session:
        session.add(Ticker(symbol="COLD"))
        await session.commit()
    assert (await client.get("/tickers/COLD/filings")).status_code == 404  # held, never ingested
    assert (await client.get("/tickers/not a symbol/earnings")).status_code == 422
