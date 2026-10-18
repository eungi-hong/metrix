"""Filings from EDGAR, stored and attached to movements.

`attach_filings` is called when a movement is enriched (and by the analytics
backfill): it makes sure the ticker has a CIK, reads its filing list (cached
for SEC_SUBMISSIONS_TTL_HOURS, so a night's enrichments share one request),
stores any new tracked filings once (unique on accession), and attaches those
accepted inside the movement's news window, with their session timing.

EDGAR is optional and best effort: disabled (the default), unconfigured or
unreachable, this returns what is already stored and never fails the
enrichment it is part of. Filings are free, so no spend is involved; the
rate limit and caching keep the SEC's fair-access rules.
"""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import MetrixError
from app.core.logging import get_logger
from app.models.filings import Filing, MovementFiling
from app.models.market import Movement, Ticker
from app.services import edgar, sessions
from app.services.news.queries import search_window

logger = get_logger(__name__)

# Filings this far outside the news window are still attached: a filing late
# on the window's last evening is still evidence for, or against, the move.
ATTACH_MARGIN = timedelta(hours=12)


async def _ensure_cik(session: AsyncSession, ticker: Ticker) -> int | None:
    if ticker.cik is None:
        ticker.cik = await edgar.cik_for(ticker.symbol, session=session)
    return ticker.cik


async def _store(session: AsyncSession, ticker: Ticker, parsed: list[edgar.Filing]) -> None:
    known = set(
        (await session.scalars(sa.select(Filing.accession).where(Filing.accession.in_([f.accession for f in parsed])))).all()
    )
    for filing in parsed:
        if filing.accession in known:
            continue
        # A savepoint per new filing: another worker enriching a different
        # movement of the same company may store the same accession first, and
        # losing that race must not fail this enrichment.
        try:
            async with session.begin_nested():
                session.add(
                    Filing(
                        ticker_id=ticker.id, cik=filing.cik or (ticker.cik or 0), accession=filing.accession,
                        form=filing.form, filing_date=filing.filing_date, accepted_at=filing.accepted_at,
                        items=list(filing.items), primary_document=filing.primary_document, url=filing.url,
                    )
                )
        except IntegrityError:
            logger.info("filing_stored_concurrently", accession=filing.accession)


async def attach_filings(session: AsyncSession, ticker: Ticker, movement: Movement) -> list[MovementFiling]:
    """Refresh the ticker's filings (if EDGAR is enabled) and attach those in
    the movement's window. Returns the attachments. Flushes."""
    if settings.sec_edgar_enabled and not ticker.is_benchmark:
        try:
            cik = await _ensure_cik(session, ticker)
            if cik is not None:
                await _store(session, ticker, await edgar.filings_for(cik, session=session))
        except (MetrixError, edgar.EdgarError) as exc:
            logger.warning("filings_unavailable", symbol=ticker.symbol, error=str(exc))

    start, end = search_window(movement.date, movement.news_window_mode)
    rows = (
        await session.scalars(
            sa.select(Filing).where(
                Filing.ticker_id == ticker.id,
                Filing.accepted_at >= start - ATTACH_MARGIN,
                Filing.accepted_at <= end + ATTACH_MARGIN,
            )
        )
    ).all()
    existing = {
        mf.filing_id: mf
        for mf in (await session.scalars(sa.select(MovementFiling).where(MovementFiling.movement_id == movement.id))).all()
    }
    frame = sessions.frame_for(movement.date)
    for filing in rows:
        if filing.id in existing:
            continue
        timing = sessions.classify(filing.accepted_at, frame)
        attachment = MovementFiling(
            movement_id=movement.id, filing_id=filing.id, timing=timing.timing, timing_precision=timing.precision
        )
        attachment.filing = filing
        session.add(attachment)
        existing[filing.id] = attachment
    await session.flush()
    return list(existing.values())
