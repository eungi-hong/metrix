"""Smoke tests for every endpoint, plus the behaviours that are easy to break:
filtering, pagination, idempotent re-ingestion, and error mapping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models.enums import IngestStatus
from app.services import ingestion
from app.core.errors import TickerNotFoundError
from app.services import prices as price_service


# ------------------------------------------------------------------- health


async def test_health_reports_configuration(client):
    response = await client.get("/health")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["news_provider"] == "fixture"
    assert body["movement_detection"]["k"] == 2.0


# ------------------------------------------------------------------ tickers


async def test_get_ticker_ingests_inline_and_returns_nested_news(client):
    response = await client.get("/tickers/TEST", params={"wait": True})
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ready"
    assert body["ticker"]["symbol"] == "TEST"
    assert body["ticker"]["company_name"] == "Test Industries Inc"
    assert body["price_range"]["bars"] == 41

    # The single -8% shock day is detected, and nothing else is.
    assert body["pagination"]["total"] == 1
    (movement,) = body["movements"]
    assert movement["daily_return_pct"] == pytest.approx(-8.0, abs=0.01)
    assert movement["direction"] == "down"
    assert movement["threshold_source"] == "volatility"
    assert movement["sigma_multiple"] > 2

    # News is nested inside the movement it explains, with tier and rationale.
    assert len(movement["news"]) == 2
    top = movement["news"][0]
    assert top["relevance_tier"] == "easy"
    assert top["relevance_score"] == pytest.approx(0.91)
    assert "earnings miss" in top["rationale"]
    assert top["article"]["url"].startswith("https://")


async def test_cold_ticker_without_wait_returns_202_and_schedules_ingestion(client):
    response = await client.get("/tickers/COLD")
    assert response.status_code == 202

    body = response.json()
    assert body["status"] == "ingesting"
    assert body["movements"] == []
    assert "wait=true" in body["message"]
    assert client.scheduled_ingestions == ["COLD"]


async def test_second_request_is_served_from_storage_without_refetching(client, monkeypatch):
    await client.get("/tickers/TEST", params={"wait": True})

    async def explode(*args, **kwargs):
        raise AssertionError("fresh data must not trigger a refetch")

    monkeypatch.setattr(price_service, "fetch_price_history", explode)

    response = await client.get("/tickers/TEST")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_unknown_ticker_returns_404(client, monkeypatch):
    async def not_found(symbol: str, days: int | None = None):
        raise TickerNotFoundError(symbol)

    monkeypatch.setattr(price_service, "fetch_price_history", not_found)

    response = await client.get("/tickers/NOPE", params={"wait": True})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


@pytest.mark.parametrize("symbol", ["12345", "TOO-LONG-SYMBOL-HERE", "%%%"])
async def test_malformed_symbols_are_rejected(client, symbol):
    assert (await client.get(f"/tickers/{symbol}")).status_code == 422


async def test_start_after_end_is_rejected(client):
    response = await client.get(
        "/tickers/TEST", params={"start": "2024-06-01", "end": "2024-01-01"}
    )
    assert response.status_code == 422


# ------------------------------------------------------------------- filters


async def test_direction_filter(client):
    await client.get("/tickers/TEST", params={"wait": True})

    down = await client.get("/tickers/TEST", params={"direction": "down"})
    up = await client.get("/tickers/TEST", params={"direction": "up"})

    assert down.json()["pagination"]["total"] == 1
    assert up.json()["pagination"]["total"] == 0


async def test_min_magnitude_filter_is_expressed_in_percent(client):
    await client.get("/tickers/TEST", params={"wait": True})

    included = await client.get("/tickers/TEST", params={"min_magnitude_pct": 5})
    excluded = await client.get("/tickers/TEST", params={"min_magnitude_pct": 9})

    assert included.json()["pagination"]["total"] == 1
    assert excluded.json()["pagination"]["total"] == 0


async def test_date_range_filter(client):
    await client.get("/tickers/TEST", params={"wait": True})

    outside = await client.get(
        "/tickers/TEST", params={"start": "2023-01-01", "end": "2023-12-31"}
    )
    assert outside.json()["pagination"]["total"] == 0


async def test_tier_filter_restricts_both_movements_and_articles(client):
    await client.get("/tickers/TEST", params={"wait": True})

    hard = await client.get("/tickers/TEST", params={"tier": "hard"})
    body = hard.json()
    assert body["pagination"]["total"] == 1
    assert [n["relevance_tier"] for n in body["movements"][0]["news"]] == ["hard"]

    medium = await client.get("/tickers/TEST", params={"tier": "medium"})
    assert medium.json()["pagination"]["total"] == 0


async def test_pagination_reports_total_independently_of_page_size(client):
    await client.get("/tickers/TEST", params={"wait": True})

    response = await client.get("/tickers/TEST", params={"limit": 1, "offset": 5})
    body = response.json()

    assert body["pagination"]["total"] == 1
    assert body["pagination"]["returned"] == 0
    assert body["movements"] == []


# --------------------------------------------------------------- idempotency


async def test_reingestion_does_not_duplicate_rows(client, session_factory, stub_llm):
    await client.get("/tickers/TEST", params={"wait": True})
    first = (await client.get("/tickers/TEST")).json()

    async with session_factory() as session:
        await ingestion.ingest_ticker(session, "TEST", llm=stub_llm)

    second = (await client.get("/tickers/TEST")).json()

    assert second["pagination"]["total"] == first["pagination"]["total"] == 1
    assert second["price_range"]["bars"] == first["price_range"]["bars"]
    assert len(second["movements"][0]["news"]) == len(first["movements"][0]["news"])


async def test_completed_movements_are_not_rescored_on_reingest(
    session_factory, stub_llm
):
    async with session_factory() as session:
        await ingestion.ingest_ticker(session, "TEST", llm=stub_llm)
    calls_after_first = len(stub_llm.structured_calls)

    async with session_factory() as session:
        await ingestion.ingest_ticker(session, "TEST", llm=stub_llm)

    # Peers are cached and the movement's news is already complete, so the
    # second run must not spend another LLM call.
    assert len(stub_llm.structured_calls) == calls_after_first


async def test_concurrent_claim_is_granted_once(session_factory):
    async with session_factory() as session:
        ticker = await ingestion.get_or_create_ticker(session, "LOCKED")
        assert await ingestion.claim_ingestion(session, ticker) is True
        assert await ingestion.claim_ingestion(session, ticker) is False
        assert ticker.ingest_status == IngestStatus.RUNNING


# ---------------------------------------------------------------------- chat


async def test_chat_answers_from_stored_data_with_sources(client, stub_llm):
    await client.get("/tickers/TEST", params={"wait": True})

    response = await client.post(
        "/chat", json={"ticker": "TEST", "question": "Why did the stock drop?"}
    )
    assert response.status_code == 200

    body = response.json()
    assert body["ticker"] == "TEST"
    assert body["grounded"] is True
    assert body["answer"] == stub_llm.answer
    assert body["conversation_id"]

    assert [m["ref"] for m in body["sources"]["movements"]] == ["M1"]
    assert body["sources"]["articles"][0]["ref"] == "A1"

    # The prompt actually carried the retrieved data, not just the question.
    prompt = stub_llm.complete_calls[-1]["messages"][-1]["content"]
    assert "ALL MAJOR MOVEMENTS ON RECORD" in prompt
    assert "[M1]" in prompt and "[A1]" in prompt


async def test_chat_is_multi_turn(client, stub_llm):
    await client.get("/tickers/TEST", params={"wait": True})

    first = await client.post(
        "/chat", json={"ticker": "TEST", "question": "What was the biggest move?"}
    )
    conversation_id = first.json()["conversation_id"]

    second = await client.post(
        "/chat",
        json={"question": "And what caused it?", "conversation_id": conversation_id},
    )
    body = second.json()

    assert body["conversation_id"] == conversation_id
    # The ticker carried over without being restated.
    assert body["ticker"] == "TEST"

    replayed = stub_llm.complete_calls[-1]["messages"]
    assert replayed[0]["content"] == "What was the biggest move?"
    assert replayed[0]["role"] == "user"
    assert len(replayed) > 1


async def test_chat_infers_ticker_from_the_question(client):
    await client.get("/tickers/TEST", params={"wait": True})

    response = await client.post(
        "/chat", json={"question": "What happened to Test Industries this year?"}
    )
    assert response.json()["ticker"] == "TEST"


async def test_chat_without_any_data_is_not_grounded(client):
    response = await client.post(
        "/chat", json={"question": "Why did something move?"}
    )
    body = response.json()

    assert response.status_code == 200
    assert body["grounded"] is False
    assert body["sources"]["movements"] == []


async def test_chat_rejects_a_blank_question(client):
    assert (await client.post("/chat", json={"question": "   "})).status_code == 422


# --------------------------------------------------------- failed ingestion


async def test_a_failed_background_ingestion_is_reported_not_retried_forever(
    client, session_factory, monkeypatch
):
    """Regression: a permanently broken symbol reported "ingesting" on every
    poll and re-ran the whole pipeline each time, never telling the caller why.
    """
    from app.models.enums import IngestStatus
    from app.models.market import Ticker

    async with session_factory() as session:
        session.add(
            Ticker(
                symbol="BROKEN",
                ingest_status=IngestStatus.FAILED,
                ingest_error="yfinance: possibly delisted",
                ingest_started_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    response = await client.get("/tickers/BROKEN")

    assert response.status_code == 502
    body = response.json()
    assert body["status"] == "failed"
    assert "delisted" in body["message"]
    # And it did not silently kick off yet another doomed ingestion.
    assert client.scheduled_ingestions == []


async def test_refresh_forces_a_retry_of_a_failed_ticker(client, session_factory):
    from app.models.enums import IngestStatus
    from app.models.market import Ticker

    async with session_factory() as session:
        session.add(
            Ticker(
                symbol="TEST",
                ingest_status=IngestStatus.FAILED,
                ingest_error="transient upstream blip",
                ingest_started_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    response = await client.get("/tickers/TEST", params={"refresh": True, "wait": True})

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["pagination"]["total"] == 1


async def test_an_old_failure_is_retried_rather_than_reported(client, session_factory):
    """Past the staleness window, a retry is the right move."""
    from app.models.enums import IngestStatus
    from app.models.market import Ticker

    async with session_factory() as session:
        session.add(
            Ticker(
                symbol="STALE",
                ingest_status=IngestStatus.FAILED,
                ingest_error="an old, probably transient failure",
                ingest_started_at=datetime.now(timezone.utc) - timedelta(days=3),
            )
        )
        await session.commit()

    response = await client.get("/tickers/STALE")

    assert response.status_code == 202
    assert response.json()["status"] == "ingesting"
    assert client.scheduled_ingestions == ["STALE"]


# ------------------------------------------------------------- price series


async def test_prices_are_not_included_by_default(client):
    """A year of bars is ~250 rows that most callers do not want."""
    await client.get("/tickers/TEST", params={"wait": True})

    body = (await client.get("/tickers/TEST")).json()

    assert body["prices"] is None
    assert body["price_range"]["bars"] == 41


async def test_include_prices_returns_the_bars(client):
    await client.get("/tickers/TEST", params={"wait": True})

    body = (await client.get("/tickers/TEST", params={"include_prices": True})).json()

    assert len(body["prices"]) == 41
    first = body["prices"][0]
    assert set(first) == {"date", "open", "high", "low", "close", "adj_close", "volume"}
    assert isinstance(first["adj_close"], float)
    assert first["volume"] == 1_000_000


async def test_prices_are_returned_in_date_order(client):
    await client.get("/tickers/TEST", params={"wait": True})

    prices = (
        await client.get("/tickers/TEST", params={"include_prices": True})
    ).json()["prices"]

    dates = [bar["date"] for bar in prices]
    assert dates == sorted(dates)


async def test_prices_honour_the_date_filters(client):
    await client.get("/tickers/TEST", params={"wait": True})

    body = (
        await client.get(
            "/tickers/TEST",
            params={"include_prices": True, "start": "2024-01-10", "end": "2024-01-20"},
        )
    ).json()

    dates = [bar["date"] for bar in body["prices"]]
    assert dates
    assert all("2024-01-10" <= d <= "2024-01-20" for d in dates)
    # The summary still describes everything stored, not just the filtered slice.
    assert body["price_range"]["bars"] == 41
