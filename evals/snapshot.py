"""Freeze the candidate set the scorer saw for a movement into a snapshot.

The scorer saw: the three tier searches for the movement, their results
parsed and held to the window, then deduplicated. Each search's raw payload
is in `news_query_cache`, so the set is rebuilt from there: first by the
request's exact cache fingerprint, then by the same query text and window
(settings such as MAX_CANDIDATES_PER_TIER may have changed since). Any
article linked to the movement but missing from the rebuild is added, since
it must have been a candidate; the snapshot is then marked incomplete, with
a note per gap. Cache rows can be overwritten when a search is refetched
after expiry, which is why snapshots are frozen to files rather than rebuilt
later.

    DATABASE_URL=... python -m evals.snapshot --symbols NVDA,AAPL --out evals/snapshots
    # with filings around each move (needs SEC_USER_AGENT and network):
    DATABASE_URL=... SEC_USER_AGENT="..." python -m evals.snapshot --symbols NVDA --filings

Read-only on the database it is pointed at.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import NewsStatus
from app.models.market import Movement, Ticker
from app.models.news import MovementNewsLink, NewsQueryCache
from app.services import edgar, sessions
from app.services.news.base import NewsCandidate, NewsProvider, NewsSearchRequest
from app.services.news.exa import ExaNewsProvider
from app.services.news.fixture import FixtureNewsProvider
from app.services.news.queries import MovementContext, build_tier_queries, search_window
from app.services.peers import PeerSet
from app.services.relevance import deduplicate
from evals.schema import SNAPSHOTS_DIR, Snapshot, SnapshotCandidate, SnapshotFiling, candidate_id, save_snapshot

PARSERS: dict[str, type[NewsProvider]] = {"exa": ExaNewsProvider, "fixture": FixtureNewsProvider}
# Filings this far either side of the news window are kept in the snapshot.
FILING_MARGIN = timedelta(days=1)


async def _cached_payload(session: AsyncSession, request: NewsSearchRequest, provider: str) -> dict | None:
    key = request.cache_fingerprint(provider)
    row = await session.scalar(sa.select(NewsQueryCache).where(NewsQueryCache.cache_key == key))
    if row is not None:
        return row.response
    rows = (
        await session.scalars(
            sa.select(NewsQueryCache).where(NewsQueryCache.provider == provider, NewsQueryCache.query == request.query)
        )
    ).all()
    for row in rows:
        params = row.params or {}
        if params.get("start") == request.start.isoformat() and params.get("end") == request.end.isoformat():
            return row.response
    return None


def _snapshot_candidate(search_tier: str, candidate: NewsCandidate, frame: sessions.SessionFrame | None) -> SnapshotCandidate:
    timing = sessions.classify(candidate.published_at, frame)
    return SnapshotCandidate(
        id=candidate_id(candidate.url),
        url=candidate.url,
        title=candidate.title,
        source_domain=candidate.source_domain,
        published_at=candidate.published_at,
        summary=candidate.summary,
        content=candidate.content,
        search_tier=search_tier,
        timing=timing.timing,
        timing_precision=timing.precision,
    )


def _snapshot_filing(filing: edgar.Filing, frame: sessions.SessionFrame | None) -> SnapshotFiling:
    timing = sessions.classify(filing.accepted_at, frame)
    return SnapshotFiling(
        accession=filing.accession,
        form=filing.form,
        items=list(filing.items),
        events=filing.events,
        accepted_at=filing.accepted_at,
        url=filing.url,
        timing=timing.timing,
        timing_precision=timing.precision,
    )


async def build_snapshot(
    session: AsyncSession,
    movement: Movement,
    ticker: Ticker,
    *,
    provider: str = "exa",
    filings: list[edgar.Filing] | None = None,
) -> Snapshot:
    parser = PARSERS[provider]()
    peers = PeerSet.model_validate(ticker.peers) if ticker.peers else PeerSet(peers=[], industry_themes=[])
    context = MovementContext(
        symbol=ticker.symbol,
        company_name=ticker.company_name,
        sector=ticker.sector,
        industry=ticker.industry,
        movement_date=movement.date,
        daily_return=movement.daily_return,
        direction=movement.direction.value,
        window_mode=movement.news_window_mode,
    )
    frame = sessions.frame_for(movement.date)
    notes: list[str] = []
    found: list[tuple[str, NewsCandidate]] = []
    for search_tier, request in build_tier_queries(context, peers):
        payload = await _cached_payload(session, request, provider)
        if payload is None:
            notes.append(f"{search_tier} search not in the cache")
            continue
        found.extend((search_tier, c) for c in parser.enforce_window(request, parser.parse(payload)))

    candidates = [_snapshot_candidate(tier, c, frame) for tier, c in deduplicate(found)]
    seen = {c.id for c in candidates}
    for link in movement.news_links:
        cid = candidate_id(link.article.url)
        if cid in seen:
            continue
        article = link.article
        candidates.append(
            _snapshot_candidate(
                link.search_tier or "unknown",
                NewsCandidate(
                    url=article.url, title=article.title, published_at=article.published_at,
                    summary=article.summary, content=article.content, source_domain=article.source_domain,
                    provider=article.provider,
                ),
                frame,
            )
        )
        seen.add(cid)
        notes.append(f"linked article {cid} was not in the rebuilt searches; added")

    start, end = search_window(movement.date, movement.news_window_mode)
    nearby = edgar.in_window(filings or [], start - FILING_MARGIN, end + FILING_MARGIN)
    return Snapshot(
        symbol=ticker.symbol,
        date=movement.date,
        company_name=ticker.company_name,
        sector=ticker.sector,
        industry=ticker.industry,
        daily_return=movement.daily_return,
        direction=movement.direction.value,
        window_mode=movement.news_window_mode,
        peers=peers.model_dump(),
        decomposition=_decomposition(movement),
        legs={"gap": movement.gap_return, "intraday": movement.intraday_return}
        if movement.gap_return is not None
        else None,
        candidates=candidates,
        filings=[_snapshot_filing(f, frame) for f in nearby],
        complete=not notes,
        notes=notes,
        built_at=datetime.now(timezone.utc),
    )


def _decomposition(movement: Movement) -> dict | None:
    if movement.decomposition_model is None:
        return None
    return {
        "model": movement.decomposition_model,
        "market": movement.market_component,
        "sector": movement.sector_component,
        "abnormal": movement.abnormal_return,
        "alpha": movement.alpha,
        "sar": movement.sar,
        "dominant": movement.dominant_component,
    }


async def snapshot_symbols(
    session: AsyncSession, symbols: list[str], out: Path, *, provider: str, with_filings: bool
) -> list[Path]:
    """Snapshot every enriched, qualifying movement of `symbols`."""
    written = []
    for symbol in symbols:
        ticker = await session.scalar(sa.select(Ticker).where(Ticker.symbol == symbol))
        if ticker is None:
            print(f"{symbol}: not stored")
            continue
        filings: list[edgar.Filing] = []
        if with_filings:
            cik = await edgar.cik_for(symbol)
            filings = await edgar.filings_for(cik) if cik else []
        movements = (
            await session.scalars(
                sa.select(Movement)
                .where(
                    Movement.ticker_id == ticker.id,
                    Movement.qualifies.is_(True),
                    Movement.news_status.in_([NewsStatus.COMPLETE, NewsStatus.PARTIAL]),
                )
                .order_by(Movement.date)
                .options(selectinload(Movement.news_links).selectinload(MovementNewsLink.article))
            )
        ).all()
        for movement in movements:
            snapshot = await build_snapshot(session, movement, ticker, provider=provider, filings=filings)
            written.append(save_snapshot(snapshot, out))
            print(f"{snapshot.key}: {len(snapshot.candidates)} candidates, {len(snapshot.filings)} filings"
                  + ("" if snapshot.complete else f" (incomplete: {'; '.join(snapshot.notes)})"))
    return written


def main(argv: list[str] | None = None) -> int:
    from app.db.session import SessionLocal, dispose_engine

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--out", type=Path, default=SNAPSHOTS_DIR)
    parser.add_argument("--provider", default="exa", choices=sorted(PARSERS))
    parser.add_argument("--filings", action="store_true", help="fetch filings from EDGAR (network)")
    args = parser.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    async def go() -> int:
        try:
            async with SessionLocal() as session:
                written = await snapshot_symbols(session, symbols, args.out, provider=args.provider, with_filings=args.filings)
        finally:
            await dispose_engine()
        print(f"wrote {len(written)} snapshots to {args.out}")
        return 0

    return asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(main())
