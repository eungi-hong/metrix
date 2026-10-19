"""SEC EDGAR: which filings a company made, and when they were accepted.

Filings are primary sources. An 8-K with Item 2.02 accepted at 16:20 New York
time is the earnings release behind the next day's gap, with a timestamp no
news article can beat. Two public JSON endpoints are enough:

* ``https://www.sec.gov/files/company_tickers.json`` maps tickers to CIKs;
* ``https://data.sec.gov/submissions/CIK##########.json`` (ten digits, zero
  padded) lists a company's filings: ``form``, ``filingDate``,
  ``acceptanceDateTime``, ``items``, ``accessionNumber``, ``primaryDocument``,
  as parallel arrays under ``filings.recent``.

Fair access, as the SEC asks: a ``User-Agent`` naming the requester
(``SEC_USER_AGENT``, required), a rate well under 10 requests per second (its
own token bucket, ``SEC_MAX_RPS``), and caching, since a filing never changes
once accepted. Requests go only to SEC hosts, follow no redirects, and refuse
oversized responses.

``acceptanceDateTime`` arrives as ``2024-05-22T16:20:43.000Z``. The trailing
Z suggests UTC, but the values read as New York wall time (an after-close
earnings release at 16:20 UTC would be 12:20 in New York, mid-session). It is
read in ``SEC_ACCEPTANCE_TIMEZONE`` (New York by default) and converted to
UTC. **This has not been verified against a live filing in this repository**;
docs/EVALUATION.md gives the check, and a test pins the interpretation so a
change is deliberate.

Parsing is pure and tested on fixtures; fetching is a thin layer on top.
Phase 2 builds the module; Phase 3 wires it into ingestion.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import ConfigurationError, MetrixError
from app.core.logging import get_logger
from app.models.news import NewsQueryCache
from app.services import ratelimit

logger = get_logger(__name__)

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
ALLOWED_HOSTS = frozenset({"www.sec.gov", "sec.gov", "data.sec.gov"})
CACHE_PROVIDER = "sec"

# 8-K items, as readable events (Form 8-K General Instructions).
ITEM_EVENTS: dict[str, str] = {
    "1.01": "material agreement",
    "1.02": "termination of a material agreement",
    "2.01": "acquisition or disposition completed",
    "2.02": "results of operations (earnings)",
    "2.05": "restructuring",
    "2.06": "impairment",
    "3.01": "delisting notice",
    "4.02": "non-reliance on financial statements (restatement)",
    "5.02": "officer or director change",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
}
EARNINGS_ITEM = "2.02"

# Forms worth tracking besides 8-K. Schedule 13D is filed as "SC 13D" in
# older filings and "SCHEDULE 13D" since EDGAR's 2024 beneficial ownership
# changes; both are matched (UNVERIFIED against live data here).
TRACKED_FORMS = ("8-K", "10-Q", "10-K", "S-3", "SC 13D", "SCHEDULE 13D")
TRACKED_PREFIXES = ("424B",)


class EdgarError(MetrixError):
    """EDGAR could not be read."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"SEC EDGAR: {detail}")


@dataclass(frozen=True, slots=True)
class Filing:
    cik: int
    accession: str  # "0001045810-24-000113"
    form: str
    filing_date: date
    accepted_at: datetime | None  # UTC
    items: tuple[str, ...]
    primary_document: str | None

    @property
    def events(self) -> list[str]:
        return [ITEM_EVENTS[item] for item in self.items if item in ITEM_EVENTS]

    @property
    def is_earnings(self) -> bool:
        return self.form.startswith("8-K") and EARNINGS_ITEM in self.items

    @property
    def url(self) -> str | None:
        if not self.primary_document:
            return None
        return ARCHIVES_URL.format(
            cik=self.cik, accession=self.accession.replace("-", ""), document=self.primary_document
        )


def is_tracked(form: str) -> bool:
    base = form.removesuffix("/A")
    return base in TRACKED_FORMS or base.startswith(TRACKED_PREFIXES)


# ------------------------------------------------------------------ parsing


def parse_company_tickers(payload: dict[str, Any]) -> dict[str, int]:
    """Ticker to CIK. EDGAR writes class shares with a hyphen (BRK-B), as Yahoo does."""
    out: dict[str, int] = {}
    for row in payload.values():
        try:
            out[str(row["ticker"]).strip().upper()] = int(row["cik_str"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_acceptance(value: str | None, tz: str | None = None) -> datetime | None:
    """`acceptanceDateTime` as a UTC instant, read as wall time in `tz`."""
    if not value:
        return None
    try:
        wall = datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None
    zone = ZoneInfo(tz or settings.sec_acceptance_timezone)
    return wall.replace(tzinfo=zone).astimezone(timezone.utc)


def parse_submissions(payload: dict[str, Any], *, tracked_only: bool = True) -> list[Filing]:
    """The filings in a submissions document's `filings.recent`, newest first."""
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    cik = int(payload.get("cik") or 0)

    def column(name: str) -> list[Any]:
        values = recent.get(name) or []
        return values + [None] * (len(forms) - len(values))

    filings: list[Filing] = []
    for form, accession, filed, accepted, items, document in zip(
        forms,
        column("accessionNumber"),
        column("filingDate"),
        column("acceptanceDateTime"),
        column("items"),
        column("primaryDocument"),
    ):
        if not form or not accession or (tracked_only and not is_tracked(form)):
            continue
        try:
            filing_date = date.fromisoformat(filed)
        except (TypeError, ValueError):
            continue
        filings.append(
            Filing(
                cik=cik,
                accession=accession,
                form=form,
                filing_date=filing_date,
                accepted_at=parse_acceptance(accepted),
                items=tuple(i.strip() for i in (items or "").split(",") if i.strip()),
                primary_document=document or None,
            )
        )
    filings.sort(key=lambda f: (f.accepted_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return filings


def in_window(filings: list[Filing], start: datetime, end: datetime) -> list[Filing]:
    """Filings accepted inside [start, end]; those with no time by filing date."""
    out = []
    for filing in filings:
        if filing.accepted_at is not None:
            if start <= filing.accepted_at <= end:
                out.append(filing)
        elif start.date() <= filing.filing_date <= end.date():
            out.append(filing)
    return out


# ----------------------------------------------------------------- fetching


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise EdgarError(f"refusing to fetch {url!r}: not an SEC host")


def user_agent() -> str:
    agent = (settings.sec_user_agent or "").strip()
    if not agent:
        raise ConfigurationError("SEC_USER_AGENT is not set; EDGAR requires a contact in the User-Agent.")
    return agent


async def fetch_json(
    url: str,
    *,
    session: AsyncSession | None = None,
    ttl: timedelta,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """GET an SEC JSON document, through the cache when a session is given."""
    _check_url(url)
    key = hashlib.sha256(f"{CACHE_PROVIDER}:{url}".encode()).hexdigest()
    now = datetime.now(timezone.utc)
    if session is not None:
        cached = await session.scalar(sa.select(NewsQueryCache).where(NewsQueryCache.cache_key == key))
        if cached is not None and _as_utc(cached.expires_at) > now:
            return cached.response

    await ratelimit.bucket("sec").acquire()
    owns = client is None
    client = client or httpx.AsyncClient(timeout=settings.sec_http_timeout_seconds, follow_redirects=False)
    try:
        response = await client.get(url, headers={"User-Agent": user_agent(), "Accept": "application/json"})
    except httpx.RequestError as exc:
        raise EdgarError(f"request failed: {exc}") from exc
    finally:
        if owns:
            await client.aclose()
    if response.status_code != 200:
        raise EdgarError(f"HTTP {response.status_code} for {url}")
    if len(response.content) > settings.sec_max_response_bytes:
        raise EdgarError(f"response over {settings.sec_max_response_bytes} bytes for {url}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise EdgarError(f"not JSON: {url}") from exc

    if session is not None:
        row = await session.scalar(sa.select(NewsQueryCache).where(NewsQueryCache.cache_key == key))
        if row is None:
            session.add(
                NewsQueryCache(
                    cache_key=key, provider=CACHE_PROVIDER, query=url, params={"url": url},
                    response=payload, expires_at=now + ttl,
                )
            )
        else:
            row.response, row.expires_at = payload, now + ttl
        await session.flush()
    return payload


async def cik_for(symbol: str, *, session: AsyncSession | None = None, client: httpx.AsyncClient | None = None) -> int | None:
    payload = await fetch_json(
        COMPANY_TICKERS_URL,
        session=session,
        ttl=timedelta(days=settings.sec_company_tickers_ttl_days),
        client=client,
    )
    return parse_company_tickers(payload).get(symbol.strip().upper().replace(".", "-"))


async def filings_for(
    cik: int, *, session: AsyncSession | None = None, client: httpx.AsyncClient | None = None
) -> list[Filing]:
    payload = await fetch_json(
        SUBMISSIONS_URL.format(cik=cik),
        session=session,
        ttl=timedelta(hours=settings.sec_submissions_ttl_hours),
        client=client,
    )
    return parse_submissions(payload)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
