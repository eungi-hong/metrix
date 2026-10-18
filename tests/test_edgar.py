"""SEC EDGAR parsing and fair access, offline.

The fixtures are SYNTHETIC (named so): hand-built from EDGAR's documented
JSON layout because sec.gov was unreachable where this was written. Recorded
responses should replace them; docs/EVALUATION.md has the command.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.core.config import Settings, settings
from app.core.errors import ConfigurationError
from app.services import edgar, sessions

FIXTURES = Path(__file__).parent / "fixtures" / "sec"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_tickers_map_to_ciks_and_bad_rows_are_skipped():
    ciks = edgar.parse_company_tickers(load("company_tickers.synthetic.json"))
    assert ciks == {"NVDA": 1045810, "BRK-B": 1067983}


def test_acceptance_time_is_read_as_new_york_wall_time():
    """Pinned interpretation (UNVERIFIED against a live filing; see the module).
    16:20 in New York in May is 20:20 UTC: after the close, as an earnings
    release is. Read as UTC it would be 12:20 New York, mid-session."""
    accepted = edgar.parse_acceptance("2024-05-22T16:20:43.000Z", "America/New_York")
    assert accepted == datetime(2024, 5, 22, 20, 20, 43, tzinfo=timezone.utc)
    assert edgar.parse_acceptance("2024-12-02T16:20:43.000Z", "America/New_York").hour == 21  # EST
    assert edgar.parse_acceptance("2024-05-22T16:20:43.000Z", "UTC").hour == 16
    assert edgar.parse_acceptance("", "UTC") is None and edgar.parse_acceptance("garbage", "UTC") is None


def test_submissions_parse_into_tracked_filings_newest_first(monkeypatch):
    monkeypatch.setattr(settings, "sec_acceptance_timezone", "America/New_York")
    filings = edgar.parse_submissions(load("submissions_nvda.synthetic.json"))

    assert [f.form for f in filings] == ["10-Q", "8-K", "8-K", "SCHEDULE 13D/A", "424B2"]  # Form 4 not tracked
    earnings = next(f for f in filings if f.is_earnings)
    assert earnings.accession == "0001045810-24-000112"
    assert earnings.items == ("2.02", "9.01")
    assert earnings.events == ["results of operations (earnings)"]
    assert earnings.cik == 1045810
    assert earnings.url == "https://www.sec.gov/Archives/edgar/data/1045810/000104581024000112/nvda-20240522.htm"
    officer = next(f for f in filings if f.items == ("5.02",))
    assert officer.events == ["officer or director change"]
    assert filings[-1].accepted_at is None  # no acceptance time: kept, by date
    assert len(edgar.parse_submissions(load("submissions_nvda.synthetic.json"), tracked_only=False)) == 6


def test_an_after_close_earnings_8k_is_overnight_for_the_next_session():
    filings = edgar.parse_submissions(load("submissions_nvda.synthetic.json"))
    earnings = next(f for f in filings if f.is_earnings)
    timing = sessions.classify(earnings.accepted_at, sessions.frame_for(date(2024, 5, 23)))
    assert (timing.timing, timing.precision) == ("overnight", "time")


def test_filings_in_a_window():
    filings = edgar.parse_submissions(load("submissions_nvda.synthetic.json"))
    start = datetime(2024, 5, 22, tzinfo=timezone.utc)
    window = edgar.in_window(filings, start, start + timedelta(days=1))
    assert {f.accession for f in window} == {"0001045810-24-000112", "0001045810-24-000110"}
    undated = edgar.in_window(filings, datetime(2024, 2, 21, tzinfo=timezone.utc), datetime(2024, 2, 21, 23, tzinfo=timezone.utc))
    assert [f.form for f in undated] == ["424B2"]


@pytest.mark.parametrize("form", ["8-K", "8-K/A", "10-K", "10-Q", "S-3", "424B5", "SC 13D", "SCHEDULE 13D/A"])
def test_tracked_forms(form):
    assert edgar.is_tracked(form)


@pytest.mark.parametrize("form", ["4", "3", "DEF 14A", "SC 13G"])
def test_untracked_forms(form):
    assert not edgar.is_tracked(form)


@pytest.mark.parametrize(
    "url",
    ["http://www.sec.gov/files/company_tickers.json", "https://evil.example/x.json", "https://sec.gov.evil.example/x"],
)
async def test_only_sec_hosts_over_https_are_fetched(url):
    with pytest.raises(edgar.EdgarError, match="not an SEC host"):
        await edgar.fetch_json(url, ttl=timedelta(hours=1))


async def test_requests_name_the_requester_and_never_follow_redirects(monkeypatch):
    monkeypatch.setattr(settings, "sec_user_agent", "Metrix tests test@example.com")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        with pytest.raises(edgar.EdgarError, match="HTTP 302"):
            await edgar.fetch_json(edgar.COMPANY_TICKERS_URL, ttl=timedelta(days=1), client=client)
    assert seen[0].headers["user-agent"] == "Metrix tests test@example.com"


async def test_a_missing_user_agent_refuses(monkeypatch):
    monkeypatch.setattr(settings, "sec_user_agent", None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
        with pytest.raises(ConfigurationError):
            await edgar.fetch_json(edgar.COMPANY_TICKERS_URL, ttl=timedelta(days=1), client=client)


async def test_oversized_responses_are_refused(monkeypatch):
    monkeypatch.setattr(settings, "sec_user_agent", "ua test@example.com")
    monkeypatch.setattr(settings, "sec_max_response_bytes", 10)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"x": "y" * 50}))) as client:
        with pytest.raises(edgar.EdgarError, match="over 10 bytes"):
            await edgar.fetch_json(edgar.COMPANY_TICKERS_URL, ttl=timedelta(days=1), client=client)


async def test_responses_are_cached(session, monkeypatch):
    monkeypatch.setattr(settings, "sec_user_agent", "ua test@example.com")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=load("company_tickers.synthetic.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await edgar.cik_for("brk.b", session=session, client=client) == 1067983
        assert await edgar.cik_for("NVDA", session=session, client=client) == 1045810
    assert len(calls) == 1


def test_enabling_edgar_requires_a_user_agent(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_ENABLED", "true")
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(ValueError, match="SEC_USER_AGENT"):
        Settings()
