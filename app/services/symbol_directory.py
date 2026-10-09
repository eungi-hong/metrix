"""The directory of listed US symbols: unknown symbols cost nothing.

A well-formed symbol is not a real one. Before this, `GET /tickers/QWZX` passed
the regex and cost a yfinance call, a failed ingestion and a demand hit. Now a
symbol must be in the directory (or allowed another way) before anything
external is called for it, or it is 404 at once and records no demand.

Source
------
Nasdaq Trader publishes two pipe-delimited files, refreshed daily:
`nasdaqlisted.txt` (Nasdaq) and `otherlisted.txt` (NYSE, NYSE American, NYSE
Arca, Cboe, IEX and others). Each starts with a header line and ends with a
`File Creation Time: ...` line; rows flagged `Test Issue = Y` are not real
listings. They are fetched weekly by a `refresh_symbol_directory` job.

The files cover US listings only. Foreign listings in Yahoo's form (`RY.TO`,
`VOD.L`) are allowed through SYMBOL_ALLOWLIST.

Symbol forms
------------
The files write a class share with a dot and a preferred with a dollar sign
(`BRK.B`, `ABR$D`); Yahoo, which ingestion fetches from, writes `BRK-B` and
`ABR-PD`. The directory stores Yahoo's form. A request for `BRK.B` is
recognised and served as `BRK-B`. (Units and warrants, `.U` and `.W` in the
files, are stored by the same rule, `-U` and `-W`; Yahoo's own spellings for
those vary, so they may not resolve.)

A refresh never empties the table
---------------------------------
A failed download leaves the table as it was: the job fails and is retried.
A download that succeeds but looks wrong (no trailer line, so truncated;
fewer than SYMBOL_DIRECTORY_MIN_ROWS rows; or a drop of more than
SYMBOL_DIRECTORY_MAX_SHRINK of the current table) is refused the same way.
Only then is the table replaced, in one transaction, so readers see the old
directory or the new one, never a half-written one.

Modes
-----
SYMBOL_DIRECTORY_MODE=enforce refuses unknown symbols; `warn` serves them and
logs `symbol_rejected`; `off` skips the check. `enforce` behaves as `warn`
while the table is empty, so a fresh deploy works before its first refresh.
Symbols already ingested successfully, and the seed list, are always allowed.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.symbols import normalize_symbol
from app.models.market import Ticker
from app.models.symbols import ListedSymbol
from app.services.demand import load_seed_symbols

logger = get_logger(__name__)

TRAILER = "File Creation Time"
NASDAQ_EXCHANGE = "Q"
# How long "is the directory populated?" is cached per process: it only
# changes once, on the first refresh.
POPULATED_CACHE_SECONDS = 60.0

# file name -> (symbol column, exchange column or None, test-issue column)
_LAYOUTS = {
    "nasdaqlisted": ("Symbol", None, "Test Issue"),
    "otherlisted": ("ACT Symbol", "Exchange", "Test Issue"),
}


class DirectoryFormatError(ValueError):
    """A file is not in the expected format, or was cut short."""


class DirectoryRefreshRefused(RuntimeError):
    """A download that parsed, but would shrink the directory implausibly."""


@dataclass(frozen=True, slots=True)
class Listing:
    symbol: str  # canonical (Yahoo) form
    name: str | None
    exchange: str | None
    is_etf: bool
    source: str


# ----------------------------------------------------------------- forms


def canonical(directory_symbol: str) -> str:
    """`BRK.B` -> `BRK-B`, `ABR$D` -> `ABR-PD`: the files' form to Yahoo's."""
    return normalize_symbol(directory_symbol).replace("$", "-P").replace(".", "-")


def directory_form(symbol: str) -> str:
    """`BRK-B` -> `BRK.B`, `ABR-PD` -> `ABR$D`: Yahoo's form back to the files'.

    A two-letter suffix starting with P is a preferred series; anything
    else after the hyphen is a class.
    """
    symbol = normalize_symbol(symbol)
    base, dash, suffix = symbol.partition("-")
    if not dash:
        return symbol
    if len(suffix) == 2 and suffix.startswith("P"):
        return f"{base}${suffix[1]}"
    return f"{base}.{suffix}"


# --------------------------------------------------------------- parsing


def parse(text: str, source: str) -> list[Listing]:
    """Rows of one Nasdaq Trader file, test issues dropped."""
    if source not in _LAYOUTS:
        raise DirectoryFormatError(f"unknown directory file '{source}'")
    symbol_col, exchange_col, test_col = _LAYOUTS[source]
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise DirectoryFormatError(f"{source}: empty")
    if not lines[-1].startswith(TRAILER):
        raise DirectoryFormatError(f"{source}: no '{TRAILER}' line; the download was cut short")
    header = lines[0].split("|")
    required = {symbol_col, "Security Name", "ETF", test_col}
    if exchange_col:
        required.add(exchange_col)
    missing = required - set(header)
    if missing:
        raise DirectoryFormatError(f"{source}: header lacks {sorted(missing)}")

    listings = []
    for line in lines[1:-1]:
        row = dict(zip(header, line.split("|")))
        raw = (row.get(symbol_col) or "").strip()
        if not raw or row.get(test_col, "").strip() == "Y":
            continue
        listings.append(
            Listing(
                symbol=canonical(raw),
                name=(row.get("Security Name") or "").strip() or None,
                exchange=(
                    (row.get(exchange_col) or "").strip() or None
                    if exchange_col
                    else NASDAQ_EXCHANGE
                ),
                is_etf=row.get("ETF", "").strip() == "Y",
                source=source,
            )
        )
    return listings


def _source_name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".txt")


async def download() -> dict[str, str]:
    """Every configured file, by name. Raises if any one fails: a partial
    directory would refuse everything in the missing file."""
    urls = [url.strip() for url in settings.symbol_directory_urls.split(",") if url.strip()]
    async with httpx.AsyncClient(timeout=settings.symbol_directory_timeout_seconds) as client:
        texts = {}
        for url in urls:
            response = await client.get(url)
            response.raise_for_status()
            texts[_source_name(url)] = response.text
    return texts


# --------------------------------------------------------------- refresh


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    before: int
    after: int


async def refresh(
    session: AsyncSession,
    *,
    fetch: Callable[[], Awaitable[dict[str, str]]] = download,
    now: datetime | None = None,
) -> RefreshOutcome:
    """Replace the directory with a fresh download, or leave it untouched.

    Raises (and changes nothing) if the download fails, a file is malformed,
    or the result fails the size checks in the module docstring. Commits.
    """
    now = now or datetime.now(timezone.utc)
    texts = await fetch()
    by_symbol: dict[str, Listing] = {}
    for source, text in texts.items():
        for listing in parse(text, source):
            by_symbol.setdefault(listing.symbol, listing)

    before = await session.scalar(sa.select(sa.func.count()).select_from(ListedSymbol)) or 0
    after = len(by_symbol)
    floor = max(settings.symbol_directory_min_rows, before * (1 - settings.symbol_directory_max_shrink))
    if after < floor:
        logger.error(
            "symbol_directory_refresh_refused", before=before, after=after, floor=round(floor)
        )
        raise DirectoryRefreshRefused(
            f"the download has {after} symbols, fewer than the {round(floor)} expected; "
            f"keeping the current {before}"
        )

    await session.execute(sa.delete(ListedSymbol))
    await session.execute(
        sa.insert(ListedSymbol),
        [
            {
                "symbol": listing.symbol,
                "name": listing.name[:400] if listing.name else None,
                "exchange": listing.exchange,
                "is_etf": listing.is_etf,
                "source": listing.source,
                "refreshed_at": now,
            }
            for listing in by_symbol.values()
        ],
    )
    await session.commit()
    reset_cache()
    logger.info("symbol_directory_refreshed", before=before, after=after)
    return RefreshOutcome(before=before, after=after)


# ----------------------------------------------------------------- check


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    symbol: str  # the form to serve it under (BRK.B becomes BRK-B)
    reason: str  # listed, allowlist, seed, ingested, unchecked, unlisted


_populated: tuple[float, bool] | None = None


def reset_cache() -> None:
    global _populated
    _populated = None


async def _is_populated(session: AsyncSession) -> bool:
    global _populated
    now = time.monotonic()
    if _populated is None or now - _populated[0] > POPULATED_CACHE_SECONDS:
        exists = await session.scalar(sa.select(sa.literal(True)).select_from(ListedSymbol).limit(1))
        _populated = (now, bool(exists))
    return _populated[1]


def allowlist() -> set[str]:
    return {normalize_symbol(s) for s in settings.symbol_allowlist.split(",") if s.strip()}


async def check(session: AsyncSession, symbol: str) -> Verdict:
    """Whether `symbol` may be served, and under which form. No external calls."""
    symbol = normalize_symbol(symbol)
    mode = settings.symbol_directory_mode
    if mode == "off":
        return Verdict(True, symbol, "unchecked")
    if symbol in allowlist():
        return Verdict(True, symbol, "allowlist")

    for candidate in dict.fromkeys([symbol, canonical(symbol)]):
        if await session.get(ListedSymbol, candidate) is not None:
            return Verdict(True, candidate, "listed")

    if symbol in load_seed_symbols():
        return Verdict(True, symbol, "seed")
    ingested = await session.scalar(
        sa.select(Ticker.id).where(
            Ticker.symbol == symbol,
            Ticker.last_ingested_at.is_not(None),
            Ticker.ingest_error_permanent.is_(False),
        )
    )
    if ingested is not None:
        return Verdict(True, symbol, "ingested")

    if mode == "enforce" and await _is_populated(session):
        logger.info("symbol_rejected", symbol=symbol, mode="enforce")
        return Verdict(False, symbol, "unlisted")
    logger.info("symbol_rejected", symbol=symbol, mode="warn")
    return Verdict(True, symbol, "unlisted")
