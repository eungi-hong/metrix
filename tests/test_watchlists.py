"""Watchlists: ownership, validation, and the digest."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import sessions
from tests.conftest import api_app, api_client, new_api_key
from tests.test_finance_api import ready  # noqa: F401  (fixture)


@pytest.fixture
async def other(session_factory, stub_llm):
    _, key = await new_api_key(session_factory, name="Someone Else")
    async with api_client(api_app(session_factory, stub_llm), key) as http:
        yield http


@pytest.fixture
async def anonymous(session_factory, stub_llm, monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    async with api_client(api_app(session_factory, stub_llm)) as http:
        yield http


async def test_create_read_replace_delete(client):
    created = await client.post("/watchlists", json={"name": " Chips ", "holdings": [{"symbol": "nvda"}, {"symbol": "AMD"}]})
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Chips" and body["weighted"] is False
    assert [(h["symbol"], h["weight"]) for h in body["holdings"]] == [("NVDA", 0.5), ("AMD", 0.5)]

    wid = body["id"]
    assert [w["id"] for w in (await client.get("/watchlists")).json()] == [wid]
    replaced = await client.put(f"/watchlists/{wid}", json={"name": "Weighted", "holdings": [
        {"symbol": "AMD", "weight": 3}, {"symbol": "INTC", "weight": 1}]})
    assert replaced.status_code == 200
    assert [(h["symbol"], h["weight"]) for h in replaced.json()["holdings"]] == [("AMD", 0.75), ("INTC", 0.25)]
    assert replaced.json()["weighted"] is True

    assert (await client.delete(f"/watchlists/{wid}")).status_code == 204
    assert (await client.get(f"/watchlists/{wid}")).status_code == 404


async def test_another_caller_cannot_see_or_change_a_list(client, other, anonymous):
    wid = (await client.post("/watchlists", json={"name": "Mine", "holdings": []})).json()["id"]
    for http in (other, anonymous):
        assert (await http.get(f"/watchlists/{wid}")).status_code == 404
        assert (await http.put(f"/watchlists/{wid}", json={"name": "x"})).status_code == 404
        assert (await http.delete(f"/watchlists/{wid}")).status_code == 404
        assert (await http.get(f"/watchlists/{wid}/digest")).status_code == 404
        assert (await http.get("/watchlists")).json() == []
    assert (await client.get(f"/watchlists/{wid}")).status_code == 200


async def test_an_anonymous_caller_owns_their_own_lists(anonymous):
    wid = (await anonymous.post("/watchlists", json={"name": "Anon", "holdings": [{"symbol": "AAPL"}]})).json()["id"]
    assert (await anonymous.get(f"/watchlists/{wid}")).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "holdings": []},
        {"name": "   ", "holdings": []},
        {"name": "x", "holdings": [{"symbol": "AMD", "weight": 1}, {"symbol": "NVDA"}]},  # some weighted
        {"name": "x", "holdings": [{"symbol": "AMD", "weight": 0}]},
        {"name": "x", "holdings": [{"symbol": "AMD"}, {"symbol": "amd"}]},
        {"name": "x", "holdings": [{"symbol": "not a symbol"}]},
        {"name": "x", "holdings": [{"symbol": f"S{i}"} for i in range(51)]},
    ],
)
async def test_invalid_lists_are_refused(client, body):
    assert (await client.post("/watchlists", json=body)).status_code == 422


async def test_there_is_a_cap_on_lists(client, monkeypatch):
    from app.api.routes import watchlists

    monkeypatch.setattr(watchlists, "MAX_WATCHLISTS", 2)
    for name in ("a", "b"):
        assert (await client.post("/watchlists", json={"name": name})).status_code == 201
    assert (await client.post("/watchlists", json={"name": "c"})).status_code == 409


async def test_the_digest_weights_contributions_and_lists_moves(client, ready):  # noqa: F811
    data, company_day, _ = ready
    # SYNTH and the XLK benchmark (held as a symbol like any other), 3:1.
    wid = (await client.post("/watchlists", json={"name": "Mix", "holdings": [
        {"symbol": "SYNTH", "weight": 3}, {"symbol": "XLK", "weight": 1}, {"symbol": "AAPL", "weight": 1}]})).json()["id"]
    body = (await client.get(f"/watchlists/{wid}/digest", params={"sessions": 3})).json()

    end = data.dates[-1]
    assert body["sessions"][-1] == end.isoformat() and len(body["sessions"]) == 3
    start_index = data.dates.index(sessions.offset_session(end, -3))
    expected = {
        "SYNTH": data.stock.bars[-1].adj_close / data.stock.bars[start_index].adj_close - 1,
        "XLK": data.sector.bars[-1].adj_close / data.sector.bars[start_index].adj_close - 1,
    }
    holdings = {h["symbol"]: h for h in body["holdings"]}
    assert holdings["SYNTH"]["weight"] == pytest.approx(0.75)  # AAPL has no data: weights over the rest
    assert holdings["SYNTH"]["period_return"] == pytest.approx(expected["SYNTH"], abs=1e-6)
    assert holdings["XLK"]["contribution"] == pytest.approx(0.25 * expected["XLK"], abs=1e-6)
    assert body["period_return"] == pytest.approx(0.75 * expected["SYNTH"] + 0.25 * expected["XLK"], abs=1e-6)
    assert body["missing"] == ["AAPL"] and holdings["AAPL"]["contribution"] is None

    long = (await client.get(f"/watchlists/{wid}/digest", params={"sessions": 60})).json()
    moves = {m["date"]: m for m in {h["symbol"]: h for h in long["holdings"]}["SYNTH"]["moves"]}
    assert company_day.isoformat() >= long["sessions"][0]
    assert moves[company_day.isoformat()]["evidence"] == "explained"


async def test_an_empty_digest(client):
    wid = (await client.post("/watchlists", json={"name": "Empty"})).json()["id"]
    body = (await client.get(f"/watchlists/{wid}/digest")).json()
    assert body["holdings"] == [] and body["period_return"] is None
