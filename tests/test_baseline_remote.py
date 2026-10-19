"""Tests for scripts/baseline_remote.py, offline, against a fake API.

The script turns real deployments' responses into the Phase 0 baseline
(movements per seed ticker, spend per enrichment, chat latency and citation
behaviour), so its arithmetic is pinned here on hand-made responses.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import baseline_remote as remote  # noqa: E402


def movement(day: str, status: str, links: int) -> dict:
    return {"date": day, "news_status": status, "news": [{}] * links}


def ticker_page(movements: list[dict], total: int | None = None, **extra) -> dict:
    return {
        "status": "ready",
        "job_id": None,
        "pagination": {"total": len(movements) if total is None else total},
        "price_range": {"bars": 251},
        "movements": movements,
        "warnings": [],
    } | extra


def test_movement_summary_counts_statuses_links_and_the_latest_move():
    page = ticker_page(
        [
            movement("2026-10-14", "pending", 0),
            movement("2026-08-27", "complete", 3),
            movement("2026-07-09", "complete", 2),
            movement("2026-06-18", "failed", 0),
        ]
    )

    summary = remote.summarize_movements("NVDA", [(200, page)])

    assert summary.movements_total == 4
    assert summary.by_news_status == {"complete": 2, "failed": 1, "pending": 1}
    assert summary.with_links == 2
    assert summary.links == 5
    assert summary.links_per_movement == 1.25
    assert (summary.first_date, summary.latest_date) == ("2026-06-18", "2026-10-14")
    assert (summary.latest_news_status, summary.latest_links) == ("pending", 0)
    assert summary.price_bars == 251


def test_movements_are_paged_until_the_total_is_read():
    seen_offsets: list[str] = []
    first = [movement(f"2026-01-{d:02d}", "complete", 1) for d in range(1, 29)]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        seen_offsets.append(str(offset))
        assert "authorization" not in request.headers  # anonymous, by design
        rows = first if offset == 0 else [movement("2026-02-01", "pending", 0)]
        return httpx.Response(200, json=ticker_page(rows, total=remote.PAGE_SIZE + 1))

    with httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler)) as client:
        summary = remote.fetch_movements(client, "NVDA")

    assert seen_offsets == ["0", str(remote.PAGE_SIZE)]
    assert summary.movements_total == remote.PAGE_SIZE + 1
    assert summary.latest_date == "2026-02-01"


def test_the_movements_command_fails_loudly_if_work_was_queued(monkeypatch, tmp_path, capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=ticker_page([movement("2026-10-14", "pending", 0)], job_id=99))

    real_client = httpx.Client
    monkeypatch.setattr(
        remote.httpx,
        "Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handler)),
    )
    out = tmp_path / "movements.json"

    code = remote.main(["movements", "--base-url", "https://api.test", "--symbols", "NVDA", "--json-out", str(out)])

    assert code == 1
    assert "WARNING: work was queued" in capsys.readouterr().out
    assert json.loads(out.read_text())["tickers"][0]["job_id"] == 99


def usage_day(rows: list[tuple[str, str, int, str]], background: str, interactive: str) -> dict:
    return {
        "background_usd": background,
        "interactive_usd": interactive,
        "by_operation": [
            {"provider": p, "operation": o, "calls": c, "cost_usd": cost, "estimated_calls": 0}
            for p, o, c, cost in rows
        ],
    }


def test_usage_sums_days_and_derives_spend_per_enrichment():
    days = [
        usage_day(
            [("exa", "search", 20, "0.10"), ("openai", "relevance", 8, "0.016"), ("openai", "peers", 2, "0.002")],
            background="0.118",
            interactive="0",
        ),
        usage_day(
            [("exa", "search", 10, "0.05"), ("openai", "relevance", 2, "0.004"), ("openai", "chat", 4, "0.012")],
            background="0.03",
            interactive="0.036",
        ),
    ]

    summary = remote.aggregate_usage(days)

    assert summary["enrichments_scored"] == 10
    assert summary["exa_searches"] == 30
    assert summary["exa_searches_per_enrichment"] == 3.0
    # (0.15 Exa + 0.02 relevance + 0.002 peers) / 10 relevance calls
    assert summary["spend_per_enrichment_usd"] == "0.017200"
    assert summary["relevance_cost_per_enrichment_usd"] == "0.002000"
    assert summary["exa_cost_per_search_usd"] == "0.005000"
    assert summary["spend_per_chat_turn_usd"] == "0.003000"
    assert Decimal(summary["total_usd"]) == Decimal("0.184")


def test_usage_with_no_enrichments_reports_none_rather_than_dividing_by_zero():
    summary = remote.aggregate_usage([usage_day([("openai", "chat", 1, "0.001")], "0", "0.001")])

    assert summary["enrichments_scored"] == 0
    assert summary["spend_per_enrichment_usd"] is None
    assert summary["exa_searches_per_enrichment"] is None


def test_answer_analysis_compares_cited_labels_with_returned_sources():
    body = {
        "grounded": True,
        "answer": "## Largest drop\n\n**NVDA fell 8%** on export rules [M3] [A1], and [A9].\n\n| a | b |",
        "sources": {
            "movements": [{"ref": f"M{i}"} for i in range(1, 28)],
            "articles": [{"ref": "A1"}, {"ref": "A2"}],
        },
    }

    timing = remote.analyse_answer("q", 200, 3.21, body)

    assert timing.cited == ["A1", "A9", "M3"]
    assert timing.returned_movement_sources == 27
    assert timing.returned_but_not_cited == 27  # 26 movements and A2
    assert timing.cited_but_not_returned == ["A9"]
    assert set(timing.markdown) == {"heading", "bold", "table"}
    assert timing.latency_s == 3.21


def test_a_failed_answer_keeps_the_error_and_no_analysis():
    timing = remote.analyse_answer("q", 429, 0.05, {"detail": "Over the chat_per_minute quota"})

    assert timing.error == "Over the chat_per_minute quota"
    assert timing.cited == []


def test_chat_waits_out_one_rate_limit_and_times_only_the_answer(monkeypatch):
    calls: list[int] = []
    slept: list[float] = []
    monkeypatch.setattr(remote.time, "sleep", slept.append)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "30"}, json={"detail": "slow down"})
        return httpx.Response(200, json={"answer": "[M1]", "grounded": True, "sources": {"movements": [{"ref": "M1"}]}})

    with httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler)) as client:
        timing = remote.ask(client, "q", "NVDA")

    assert slept == [30.0]
    assert timing.http_status == 200
    assert timing.cited == ["M1"]
    assert timing.returned_but_not_cited == 0


def test_an_html_error_page_from_a_proxy_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html><body>Bad gateway</body></html>")

    with httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler)) as client:
        summary = remote.fetch_movements(client, "NVDA")

    assert summary.http_status == 502
    assert summary.movements_total == 0


@pytest.mark.parametrize(("header", "seconds"), [("30", 30.0), (None, 0.0), ("soon", 0.0), ("-4", 0.0)])
def test_retry_after_is_read_as_seconds_or_ignored(header, seconds):
    assert remote.retry_after_seconds(header) == seconds


def test_usage_needs_at_least_one_day():
    with pytest.raises(SystemExit):
        remote.main(["usage", "--base-url", "https://api.test", "--days", "0"])


@pytest.mark.parametrize("questions", [remote.CHAT_QUESTIONS])
def test_the_chat_questions_are_fixed_and_name_the_ticker(questions):
    assert len(questions) == 3
    assert all(remote.CHAT_TICKER in question for question in questions)
