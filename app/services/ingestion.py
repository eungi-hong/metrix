"""The ingestion pipeline: ticker -> prices -> movements -> news -> links.

Orchestration only. The steps it calls are independently testable (the
detector is pure, the providers are swappable, the scorer takes plain data),
and this module's job is to sequence them, persist the results, and make the
whole thing idempotent and partially fault-tolerant.

Idempotency
-----------
Re-running for a ticker extends and corrects rather than duplicating. Price
bars and movements are reconciled against what is already stored (insert new,
update changed, leave the rest); articles deduplicate on a normalized-URL
hash; movement/article links carry a uniqueness constraint. A movement whose
news enrichment already succeeded is not re-enriched, so a re-run costs no
LLM calls for work already done.

Failure policy
--------------
Prices are load-bearing: if yfinance fails there is nothing to explain, and
the run fails. Everything downstream is best-effort per movement -- a news
provider timeout or an LLM error marks that one movement `news_status=failed`
and the run continues. A flaky news search must never cost the caller its
price and movement data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    MetrixError,
    NewsProviderError,
    PriceDataError,
    TickerNotFoundError,
)
from app.core.logging import get_logger
from app.db.session import session_scope
from app.models.enums import IngestStatus, NewsStatus
from app.models.market import Movement, PriceBar, Ticker
from app.models.news import MovementNewsLink, NewsArticle, url_fingerprint
from app.services import prices as price_service
from app.services.llm import LLMClient, get_llm_client
from app.services.movements import DailyReturn, DetectionParams, detect_movements
from app.services.news import NewsProvider, build_news_provider
from app.services.news.base import NewsCandidate, within_window
from app.services.news.queries import (
    MovementContext,
    build_tier_queries,
    search_window,
)
from app.services.peers import PeerSet, resolve_peers
from app.services.relevance import ScoredCandidate, score_candidates

logger = get_logger(__name__)

# A run that claimed the ticker this long ago is assumed dead (process killed
# mid-ingest) and may be reclaimed, so one crash cannot wedge a ticker forever.
STALE_CLAIM_AFTER = timedelta(minutes=15)


@dataclass(slots=True)
class IngestResult:
    symbol: str
    bars_written: int = 0
    movements_detected: int = 0
    movements_enriched: int = 0
    articles_linked: int = 0
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- staleness


def is_stale(ticker: Ticker) -> bool:
    """True when the ticker has never been ingested, or not recently enough."""
    if ticker.last_ingested_at is None or ticker.ingest_status != IngestStatus.COMPLETE:
        return True
    last = ticker.last_ingested_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last > timedelta(hours=settings.staleness_hours)


def failed_recently(ticker: Ticker) -> bool:
    """True if the last ingestion failed and it is too soon to retry it.

    Retrying a hard failure (an unknown symbol, a rejected API key) on every
    request wastes an upstream call per poll and hides the error from the
    caller, who just sees "ingesting" forever.
    """
    if ticker.ingest_status != IngestStatus.FAILED:
        return False
    attempted = ticker.ingest_started_at
    if attempted is None:
        return True
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - attempted < timedelta(
        hours=settings.staleness_hours
    )


async def get_ticker(session: AsyncSession, symbol: str) -> Ticker | None:
    return await session.scalar(
        sa.select(Ticker).where(Ticker.symbol == symbol.strip().upper())
    )


async def get_or_create_ticker(session: AsyncSession, symbol: str) -> Ticker:
    symbol = symbol.strip().upper()
    ticker = await get_ticker(session, symbol)
    if ticker is None:
        ticker = Ticker(symbol=symbol, ingest_status=IngestStatus.PENDING)
        session.add(ticker)
        await session.flush()
    return ticker


async def claim_ingestion(session: AsyncSession, ticker: Ticker) -> bool:
    """Atomically mark the ticker as being ingested. False if someone else has it.

    A single conditional UPDATE is the whole locking mechanism: it is atomic on
    any database that supports transactions, needs no advisory-lock support, and
    self-heals via `STALE_CLAIM_AFTER` if the holder dies. Two concurrent
    requests for a cold ticker therefore produce one ingestion, not two.
    """
    cutoff = datetime.now(timezone.utc) - STALE_CLAIM_AFTER
    result = await session.execute(
        sa.update(Ticker)
        .where(
            Ticker.id == ticker.id,
            sa.or_(
                Ticker.ingest_status != IngestStatus.RUNNING,
                Ticker.ingest_started_at.is_(None),
                Ticker.ingest_started_at < cutoff,
            ),
        )
        .values(
            ingest_status=IngestStatus.RUNNING,
            ingest_started_at=datetime.now(timezone.utc),
            ingest_error=None,
        )
        # Let the database evaluate the predicate. With the default the ORM
        # re-evaluates it in Python against loaded objects, which cannot
        # compare the timezone-aware cutoff to a naive stored value.
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    claimed = result.rowcount > 0
    if claimed:
        await session.refresh(ticker)
    return claimed


# ----------------------------------------------------------------- pipeline


async def ingest_ticker(
    session: AsyncSession,
    symbol: str,
    *,
    llm: LLMClient | None = None,
    news_provider: NewsProvider | None = None,
) -> IngestResult:
    """Run the full pipeline for `symbol`. Assumes the caller holds the claim."""
    symbol = symbol.strip().upper()
    llm = llm or get_llm_client()
    news_provider = news_provider or build_news_provider(session)
    result = IngestResult(symbol=symbol)

    ticker = await get_or_create_ticker(session, symbol)
    logger.info("ingest_start", symbol=symbol, ticker_id=ticker.id)

    try:
        history = await price_service.fetch_price_history(symbol)
    except (TickerNotFoundError, PriceDataError):
        ticker.ingest_status = IngestStatus.FAILED
        ticker.ingest_error = "price history unavailable"
        await session.commit()
        raise

    _apply_profile(ticker, history.profile)
    result.bars_written = await _upsert_price_bars(session, ticker, history.bars)

    params = DetectionParams.from_settings()
    detected = detect_movements([b.to_point() for b in history.bars], params)
    movements = await _upsert_movements(session, ticker, detected, params)
    result.movements_detected = len(movements)
    await session.commit()

    pending = [m for m in movements if m.news_status != NewsStatus.COMPLETE]
    budget = sorted(
        pending, key=lambda m: (m.abs_return, m.date), reverse=True
    )[: settings.max_movements_per_ingest]

    if budget:
        peers = await resolve_peers(session, ticker, llm)
        for movement in budget:
            linked = await _enrich_movement(
                session, ticker, movement, peers, news_provider, llm, result
            )
            result.articles_linked += linked
            result.movements_enriched += 1
            await session.commit()

    skipped = len(pending) - len(budget)
    if skipped > 0:
        result.warnings.append(
            f"{skipped} detected movement(s) not yet enriched with news "
            f"(MAX_MOVEMENTS_PER_INGEST={settings.max_movements_per_ingest}); "
            "re-run ingestion to process the next batch."
        )

    ticker.ingest_status = IngestStatus.COMPLETE
    ticker.ingest_error = None
    ticker.last_ingested_at = datetime.now(timezone.utc)
    await session.commit()

    logger.info(
        "ingest_complete",
        symbol=symbol,
        bars=result.bars_written,
        movements=result.movements_detected,
        enriched=result.movements_enriched,
        articles=result.articles_linked,
    )
    return result


def _apply_profile(ticker: Ticker, profile: price_service.TickerProfile) -> None:
    ticker.company_name = profile.company_name or ticker.company_name
    ticker.sector = profile.sector or ticker.sector
    ticker.industry = profile.industry or ticker.industry
    ticker.exchange = profile.exchange or ticker.exchange
    ticker.currency = profile.currency or ticker.currency


async def _upsert_price_bars(
    session: AsyncSession, ticker: Ticker, bars: list[price_service.PriceBarData]
) -> int:
    """Reconcile fetched bars against stored ones. Returns rows written.

    Reads the existing dates and diffs in Python rather than using a dialect
    specific ON CONFLICT: a year of daily bars is ~250 rows, so the simpler
    portable version costs nothing measurable and works on both Postgres and
    the SQLite database used by the tests.
    """
    existing: dict[object, PriceBar] = {
        bar.date: bar
        for bar in (
            await session.scalars(
                sa.select(PriceBar).where(PriceBar.ticker_id == ticker.id)
            )
        ).all()
    }

    written = 0
    for bar in bars:
        current = existing.get(bar.date)
        if current is None:
            session.add(
                PriceBar(
                    ticker_id=ticker.id,
                    date=bar.date,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    adj_close=bar.adj_close,
                    volume=bar.volume,
                )
            )
            written += 1
        elif float(current.adj_close) != bar.adj_close:
            # Adjusted closes are revised by splits and dividends after the fact.
            current.open, current.high, current.low = bar.open, bar.high, bar.low
            current.close, current.adj_close, current.volume = (
                bar.close,
                bar.adj_close,
                bar.volume,
            )
            written += 1

    await session.flush()
    return written


async def _upsert_movements(
    session: AsyncSession,
    ticker: Ticker,
    detected: list[DailyReturn],
    params: DetectionParams,
) -> list[Movement]:
    """Persist detected movements, preserving news already attached to them."""
    existing: dict[object, Movement] = {
        m.date: m
        for m in (
            await session.scalars(
                sa.select(Movement).where(Movement.ticker_id == ticker.id)
            )
        ).all()
    }

    kept: list[Movement] = []
    for item in detected:
        movement = existing.get(item.date)
        if movement is None:
            movement = Movement(ticker_id=ticker.id, date=item.date)
            session.add(movement)
        movement.daily_return = item.daily_return
        movement.abs_return = item.abs_return
        movement.direction = item.direction
        movement.prev_adj_close = item.prev_adj_close
        movement.adj_close = item.adj_close
        movement.volume = item.volume
        movement.rolling_std = item.rolling_std
        movement.threshold = item.threshold
        movement.threshold_source = item.threshold_source
        movement.detector_k = params.k
        movement.detector_window = params.window
        movement.detector_floor = params.floor
        kept.append(movement)

    # A day that no longer clears the threshold (revised prices, or retuned
    # parameters) stops being a movement, and its links go with it.
    detected_dates = {item.date for item in detected}
    for date_key, movement in existing.items():
        if date_key not in detected_dates:
            await session.delete(movement)

    await session.flush()
    return kept


async def _enrich_movement(
    session: AsyncSession,
    ticker: Ticker,
    movement: Movement,
    peers: PeerSet,
    news_provider: NewsProvider,
    llm: LLMClient,
    result: IngestResult,
) -> int:
    """Search, score, and link news for one movement. Never raises."""
    context = MovementContext(
        symbol=ticker.symbol,
        company_name=ticker.company_name,
        sector=ticker.sector,
        industry=ticker.industry,
        movement_date=movement.date,
        daily_return=movement.daily_return,
        direction=movement.direction.value,
    )

    try:
        candidates = await _search_all_tiers(news_provider, context, peers)
    except MetrixError as exc:
        _record_failure(movement, result, f"{movement.date}: news search: {exc}")
        return 0

    if not candidates:
        movement.news_status = NewsStatus.COMPLETE
        movement.news_fetched_at = datetime.now(timezone.utc)
        return 0

    try:
        scored = await score_candidates(context, candidates, peers, llm)
    except MetrixError as exc:
        _record_failure(movement, result, f"{movement.date}: scoring: {exc}")
        return 0

    window_start, window_end = search_window(movement.date)
    linked = 0
    for item in scored:
        article = await _upsert_article(session, item.candidate)

        # Re-check the window against the STORED article, which is the
        # authoritative record of when it was published. A search can return an
        # already-known article with its date missing, in which case the
        # provider-level filter has nothing to test; deduplication then resolves
        # it to a row whose real date is outside this movement's window.
        # Observed: a 2026-07-30 press release linked to a 2026-07-02 move.
        if not within_window(article.published_at, window_start, window_end):
            logger.info(
                "article_outside_window_skipped",
                symbol=ticker.symbol,
                movement_date=str(movement.date),
                published_at=str(article.published_at),
                url=article.url,
            )
            continue

        if await _link(session, movement, article, item):
            linked += 1

    movement.news_status = NewsStatus.COMPLETE
    movement.news_fetched_at = datetime.now(timezone.utc)
    await session.flush()
    return linked


def _record_failure(movement: Movement, result: IngestResult, message: str) -> None:
    """Mark one movement's enrichment as failed without sinking the run."""
    logger.warning("movement_enrichment_failed", detail=message)
    movement.news_status = NewsStatus.FAILED
    result.warnings.append(message)


async def _search_all_tiers(
    news_provider: NewsProvider, context: MovementContext, peers: PeerSet
) -> list[tuple[str, NewsCandidate]]:
    """Run the easy/medium/hard searches concurrently for one movement."""
    queries = build_tier_queries(context, peers)
    responses = await asyncio.gather(
        *(news_provider.search(request) for _, request in queries),
        return_exceptions=True,
    )

    candidates: list[tuple[str, NewsCandidate]] = []
    failures = 0
    for (search_tier, _), response in zip(queries, responses):
        if isinstance(response, BaseException):
            failures += 1
            logger.warning(
                "tier_search_failed",
                symbol=context.symbol,
                tier=search_tier,
                error=str(response),
            )
            continue
        candidates.extend((search_tier, candidate) for candidate in response)

    # One tier failing costs that tier's coverage; all three failing means the
    # provider is down, which the caller should record against the movement.
    if failures == len(queries):
        raise NewsProviderError(news_provider.name, "all tier searches failed")
    return candidates


async def _upsert_article(session: AsyncSession, candidate: NewsCandidate) -> NewsArticle:
    """Fetch-or-create the article row, deduplicated on normalized URL."""
    fingerprint = url_fingerprint(candidate.url)
    article = await session.scalar(
        sa.select(NewsArticle).where(NewsArticle.url_hash == fingerprint)
    )
    if article is None:
        article = NewsArticle(
            url_hash=fingerprint,
            url=candidate.url,
            title=candidate.title,
            source_domain=candidate.source_domain,
            author=candidate.author,
            published_at=candidate.published_at,
            summary=candidate.summary,
            content=candidate.content,
            provider=candidate.provider,
            raw=candidate.raw,
        )
        session.add(article)
        await session.flush()
    return article


async def _link(
    session: AsyncSession,
    movement: Movement,
    article: NewsArticle,
    item: ScoredCandidate,
) -> bool:
    """Create or refresh the movement/article link. True if newly created."""
    link = await session.scalar(
        sa.select(MovementNewsLink).where(
            MovementNewsLink.movement_id == movement.id,
            MovementNewsLink.article_id == article.id,
        )
    )
    if link is not None:
        link.relevance_tier = item.tier
        link.relevance_score = item.score
        link.rationale = item.rationale
        return False

    session.add(
        MovementNewsLink(
            movement_id=movement.id,
            article_id=article.id,
            relevance_tier=item.tier,
            relevance_score=item.score,
            rationale=item.rationale,
            search_tier=item.search_tier,
            scored_by=settings.anthropic_model,
        )
    )
    await session.flush()
    return True


# ------------------------------------------------------------ background run


async def run_ingestion_in_background(symbol: str) -> None:
    """Entry point for FastAPI BackgroundTasks -- owns its own session."""
    try:
        async with session_scope() as session:
            await ingest_ticker(session, symbol)
    except Exception as exc:
        logger.error("background_ingest_failed", symbol=symbol, error=str(exc))
        async with session_scope() as session:
            ticker = await get_ticker(session, symbol)
            if ticker is not None:
                ticker.ingest_status = IngestStatus.FAILED
                ticker.ingest_error = str(exc)[:500]
