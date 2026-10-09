"""Demand tracking, and the pre-warm universe chosen from it.

Popularity
----------
Each symbol's popularity is an exponentially decayed hit count. On every
request::

    score = score * exp(-Δt / τ) + 1,   τ = DEMAND_HALF_LIFE_DAYS / ln 2

so a request is worth 1 when it happens and half that a half-life later. A
ticker asked for daily settles near 1 / (1 - e^(-1/τ)) ≈ 10.6 with a 7-day
half-life; one asked for once a month ago has all but faded. The stored score
is as of `popularity_updated_at`, so readers decay it to "now" before
comparing -- otherwise a ticker last hit a month ago would outrank one hit
yesterday merely by having been hit more back then.

Recording is best-effort. It happens on the request path, so a failure here
is logged and swallowed: losing a hit costs a slightly worse ranking tonight,
failing the request would cost the user their answer.

The universe
------------
The nightly pre-warm covers the seed list, the `PREWARM_TOP_N` most popular
tickers, and anything requested in the last `PREWARM_RECENT_DAYS` -- minus
tickers whose last ingestion failed for a reason a retry cannot fix. Requests
for nonsense symbols therefore cost one failed ingestion each and then drop
out, rather than being retried every night.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.symbols import is_valid_symbol, normalize_symbol
from app.models.demand import TickerDemand
from app.models.enums import IngestStatus
from app.models.market import Ticker

logger = get_logger(__name__)

SECONDS_PER_DAY = 86_400


# -------------------------------------------------------------- the math


def decay_time_constant_days(half_life_days: float | None = None) -> float:
    """τ in `exp(-Δt / τ)`, from the half-life: τ = half-life / ln 2."""
    return (half_life_days or settings.demand_half_life_days) / math.log(2)


def decayed_popularity(
    score: float, as_of: datetime, now: datetime, *, half_life_days: float | None = None
) -> float:
    """`score`, recorded at `as_of`, decayed forward to `now`."""
    age_days = max((_as_utc(now) - _as_utc(as_of)).total_seconds(), 0) / SECONDS_PER_DAY
    return score * math.exp(-age_days / decay_time_constant_days(half_life_days))


# ------------------------------------------------------------- recording


async def record_demand(
    session: AsyncSession, symbol: str, *, now: datetime | None = None
) -> None:
    """Count one request for `symbol`. Never raises.

    Runs in a savepoint inside the caller's transaction and does not commit;
    the caller's commit persists it. A failure rolls back only the savepoint.
    """
    symbol = normalize_symbol(symbol)
    now = now or datetime.now(timezone.utc)
    try:
        async with session.begin_nested():
            if _dialect(session) == "postgresql":
                await _record_postgres(session, symbol, now)
            else:
                await _record_portable(session, symbol, now)
    except Exception as exc:
        logger.warning("demand_record_failed", symbol=symbol, error=str(exc))


async def _record_postgres(session: AsyncSession, symbol: str, now: datetime) -> None:
    """One atomic upsert, so concurrent hits on one symbol all count.

    The decay is computed in SQL from the row as it is at write time, not as
    some request read it earlier; a negative age (two hits whose clocks
    disagree slightly) is clamped to zero.
    """
    at = sa.literal(now, sa.DateTime(timezone=True))
    age_days = sa.func.greatest(
        sa.extract("epoch", at - TickerDemand.popularity_updated_at) / SECONDS_PER_DAY, 0
    )
    statement = pg_insert(TickerDemand).values(
        symbol=symbol,
        popularity=1.0,
        popularity_updated_at=now,
        request_count=1,
        first_requested_at=now,
        last_requested_at=now,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[TickerDemand.symbol],
            set_={
                "popularity": TickerDemand.popularity
                * sa.func.exp(-age_days / decay_time_constant_days())
                + 1,
                "popularity_updated_at": now,
                "request_count": TickerDemand.request_count + 1,
                "last_requested_at": now,
            },
        )
    )


async def _record_portable(session: AsyncSession, symbol: str, now: datetime) -> None:
    """Read, decay in Python, write. Fine for SQLite, whose writers are
    serialized anyway; not atomic against concurrent writers elsewhere."""
    row = await session.get(TickerDemand, symbol, populate_existing=True)
    if row is None:
        session.add(
            TickerDemand(
                symbol=symbol,
                popularity=1.0,
                popularity_updated_at=now,
                request_count=1,
                first_requested_at=now,
                last_requested_at=now,
            )
        )
    else:
        row.popularity = decayed_popularity(row.popularity, row.popularity_updated_at, now) + 1
        row.popularity_updated_at = now
        row.request_count += 1
        row.last_requested_at = now
    await session.flush()


# -------------------------------------------------------------- universe


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    """One ticker to pre-warm, and why."""

    symbol: str
    # Decayed to selection time; 0.0 for a seed nobody has asked for.
    popularity: float
    seeded: bool = False
    # Requested within PREWARM_RECENT_DAYS.
    recent: bool = False


async def select_prewarm_universe(
    session: AsyncSession, *, now: datetime | None = None
) -> list[UniverseEntry]:
    """Seeds ∪ top-N by decayed popularity ∪ recently requested, minus
    permanent failures. Most popular first, then alphabetical."""
    now = now or datetime.now(timezone.utc)
    seeds = load_seed_symbols()
    excluded = await _permanently_failed(session)

    demand = (await session.scalars(sa.select(TickerDemand))).all()
    popularity = {
        row.symbol: decayed_popularity(row.popularity, row.popularity_updated_at, now)
        for row in demand
        if row.symbol not in excluded
    }
    recent_cutoff = now - timedelta(days=settings.prewarm_recent_days)
    recent = {
        row.symbol
        for row in demand
        if row.symbol not in excluded and _as_utc(row.last_requested_at) >= recent_cutoff
    }
    top = sorted(popularity, key=lambda s: (-popularity[s], s))[: settings.prewarm_top_n]

    symbols = (set(seeds) - excluded) | set(top) | recent
    entries = [
        UniverseEntry(
            symbol=symbol,
            popularity=popularity.get(symbol, 0.0),
            seeded=symbol in seeds,
            recent=symbol in recent,
        )
        for symbol in symbols
    ]
    entries.sort(key=lambda e: (-e.popularity, e.symbol))
    logger.info(
        "prewarm_universe_selected",
        size=len(entries),
        seeds=len(seeds),
        popular=len(top),
        recent=len(recent),
        excluded=len(excluded),
    )
    return entries


def load_seed_symbols() -> list[str]:
    """`PREWARM_SEED_SYMBOLS` plus `PREWARM_SEED_FILE`, normalised and deduplicated.

    Invalid entries are logged and skipped rather than failing the run: a
    typo in the seed list should cost that one symbol, not the night.
    """
    raw = settings.prewarm_seed_symbols.split(",")
    path = Path(settings.prewarm_seed_file) if settings.prewarm_seed_file else None
    if path is not None and path.is_file():
        for line in path.read_text().splitlines():
            raw.append(line.split("#", 1)[0])

    seeds: list[str] = []
    for item in raw:
        symbol = normalize_symbol(item)
        if not symbol:
            continue
        if not is_valid_symbol(symbol):
            logger.warning("prewarm_seed_invalid", symbol=symbol)
            continue
        if symbol not in seeds:
            seeds.append(symbol)
    return seeds


async def _permanently_failed(session: AsyncSession) -> set[str]:
    return set(
        (
            await session.scalars(
                sa.select(Ticker.symbol).where(
                    Ticker.ingest_status == IngestStatus.FAILED,
                    Ticker.ingest_error_permanent.is_(True),
                )
            )
        ).all()
    )


def _dialect(session: AsyncSession) -> str:
    assert session.bind is not None
    return session.bind.dialect.name


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres hands back aware ones."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
