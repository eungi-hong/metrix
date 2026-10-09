"""The ingestion pipeline: ticker -> prices -> movements -> news -> links.

Orchestration only. The steps it calls are independently testable (the
detector is pure, the providers are swappable, the scorer takes plain data),
and this module's job is to sequence them, persist the results, and make the
whole thing idempotent and partially fault-tolerant.

The pipeline is two steps that also run on their own: `refresh_prices` (bars,
profile, detection, movements -- cheap, and batchable across tickers) and
`enrich_movement` (news and scoring for one movement -- the expensive part).
`ingest_ticker` is the first followed by the second over the movements its
budget allows. The worker runs them separately: prices for a whole batch of
tickers at once, then enrichment one movement per job.

Idempotency
-----------
Re-running for a ticker extends and corrects rather than duplicating. Price
bars and movements are reconciled against what is already stored (insert new,
update changed, leave the rest); articles deduplicate on a normalized-URL
hash; movement/article links carry a uniqueness constraint. A movement whose
news enrichment is COMPLETE is not re-enriched, so a re-run costs no LLM calls
for work already done.

Freshness
---------
A movement's news window runs a day past the move, so enrichment that runs
before the window closes (`Movement.news_window_closes_at`) cannot have seen
everything. Such a movement is marked PARTIAL, not COMPLETE, and is enriched
again once the window has closed. Re-enrichment re-scores the fuller candidate
set from scratch and replaces the previous verdict: links the scorer no longer
supports are removed, the rest are updated in place.

A movement that keeps failing stops being retried automatically after
`NEWS_MAX_ATTEMPTS`; only an explicit `refresh=true` tries it again.

A PARTIAL movement does not wait for someone to ask again: the pass that marks
it PARTIAL enqueues an `enrich_movement` follow-up, in the same transaction,
to run when its window closes.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import (
    MetrixError,
    NewsProviderError,
    PriceDataError,
    ProviderBusy,
    TickerNotFoundError,
    WorkDeferred,
    is_permanent,
)
from app.core.logging import get_logger
from app.models.enums import IngestStatus, NewsStatus
from app.models.jobs import JobKind, JobSource
from app.models.market import PRICE_DECIMALS, Movement, PriceBar, Ticker
from app.models.news import MovementNewsLink, NewsArticle, url_fingerprint
from app.services import prices as price_service
from app.services import queue
from app.services.llm import LLMProvider, get_llm_client
from app.services.movements import DailyReturn, DetectionParams, detect_movements
from app.services.news import NewsProvider, build_news_provider
from app.services.news.base import NewsCandidate, within_window
from app.services.news.queries import (
    MovementContext,
    build_tier_queries,
    news_window_closes_at,
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
    # The errors behind per-movement failures, so a queued job can decide
    # whether to retry. `warnings` is the human-readable side of the same.
    failures: list[MetrixError] = field(default_factory=list)
    # Set when enrichment had to stop part-way: the spend cap, or a provider
    # too busy to wait for. The prices are stored and the ticker is COMPLETE;
    # the remaining movements stay as they were. A queued job re-raises it,
    # to be held (the cap) or retried (a busy provider).
    deferred: WorkDeferred | None = None


# Handlers pass one in to publish progress on their job (`Job.progress`).
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


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


def needs_enrichment(
    movement: Movement, now: datetime, *, retry_exhausted: bool = False
) -> bool:
    """Whether a run should spend news and LLM calls on this movement.

    PARTIAL movements wait for their window to close: re-enriching earlier
    would pay again for a result that is still provisional. FAILED movements
    are retried until `NEWS_MAX_ATTEMPTS`, then only when `retry_exhausted`.
    """
    status = movement.news_status
    if status == NewsStatus.COMPLETE:
        return False
    if status == NewsStatus.PARTIAL:
        return now >= _as_utc(movement.news_window_closes_at)
    if status == NewsStatus.FAILED:
        return retry_exhausted or movement.news_attempts < settings.news_max_attempts
    return True


def needs_enrichment_clause(now: datetime) -> sa.ColumnElement[bool]:
    """`needs_enrichment` as a SQL condition (without `retry_exhausted`).

    The two must agree; a test holds them to the same answers.
    """
    return sa.or_(
        Movement.news_status == NewsStatus.PENDING,
        sa.and_(
            Movement.news_status == NewsStatus.PARTIAL,
            Movement.news_window_closes_at <= now,
        ),
        sa.and_(
            Movement.news_status == NewsStatus.FAILED,
            Movement.news_attempts < settings.news_max_attempts,
        ),
    )


async def enrichable_movements(
    session: AsyncSession, ticker: Ticker, *, limit: int, now: datetime | None = None
) -> list[Movement]:
    """The ticker's movements still owed news, largest moves first."""
    return list(
        (
            await session.scalars(
                sa.select(Movement)
                .where(Movement.ticker_id == ticker.id, needs_enrichment_clause(now or _now()))
                .order_by(Movement.abs_return.desc(), Movement.date.desc())
                .limit(limit)
            )
        ).all()
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
    llm: LLMProvider | None = None,
    news_provider: NewsProvider | None = None,
    retry_exhausted: bool = False,
    progress: ProgressCallback | None = None,
) -> IngestResult:
    """Run the full pipeline for `symbol`. Assumes the caller holds the claim.

    `retry_exhausted` also retries movements that have failed
    `NEWS_MAX_ATTEMPTS` times; it is what `refresh=true` means for news.

    If enrichment has to wait (the spend cap, a busy provider), it stops
    there, the ticker is still finished with its new prices, and the reason is
    returned in `result.deferred` rather than raised. If the price fetch
    itself has to wait, the claim is released and `ProviderBusy` raised: the
    ticker is not marked failed, since nothing failed.
    """
    report = progress or _no_progress
    symbol = symbol.strip().upper()
    llm = llm or get_llm_client()
    news_provider = news_provider or build_news_provider(session)
    result = IngestResult(symbol=symbol)

    ticker = await get_or_create_ticker(session, symbol)
    logger.info("ingest_start", symbol=symbol, ticker_id=ticker.id)
    await report({"stage": "prices"})

    try:
        history = await price_service.fetch_price_history(symbol)
    except ProviderBusy:
        await session.rollback()
        await release_claim(session, symbol)
        raise
    except (TickerNotFoundError, PriceDataError) as exc:
        ticker.ingest_status = IngestStatus.FAILED
        ticker.ingest_error = "price history unavailable"
        ticker.ingest_error_permanent = is_permanent(exc)
        await session.commit()
        raise

    refreshed = await refresh_prices(session, ticker, history)
    result.bars_written = refreshed.bars_written
    result.movements_detected = len(refreshed.movements)
    await session.commit()

    now = _now()
    pending = [
        m
        for m in refreshed.unfinished
        if needs_enrichment(m, now, retry_exhausted=retry_exhausted)
    ]
    budget = sorted(
        pending, key=lambda m: (m.abs_return, m.date), reverse=True
    )[: settings.max_movements_per_ingest]

    if budget:
        await report({"stage": "enriching", "done": 0, "total": len(budget)})
        try:
            peers = await resolve_peers(session, ticker, llm)
            for done, movement in enumerate(budget, start=1):
                linked = await _enrich_movement(
                    session, ticker, movement, peers, news_provider, llm, result
                )
                result.articles_linked += linked
                result.movements_enriched += 1
                await session.commit()
                await report({"stage": "enriching", "done": done, "total": len(budget)})
        except WorkDeferred as exc:
            # The prices are stored and committed; only news waits. Undo the
            # movement in progress (its attempt was not really made) and
            # finish the ticker below, then let the caller see why.
            await session.rollback()
            ticker = await get_or_create_ticker(session, symbol)
            result.deferred = exc
            result.warnings.append(f"News not fetched: {exc}")

    skipped = len(pending) - len(budget)
    if skipped > 0:
        result.warnings.append(
            f"{skipped} detected movement(s) not yet enriched with news "
            f"(MAX_MOVEMENTS_PER_INGEST={settings.max_movements_per_ingest}); "
            "re-run ingestion to process the next batch."
        )

    ticker.ingest_status = IngestStatus.COMPLETE
    ticker.ingest_error = None
    ticker.ingest_error_permanent = False
    ticker.last_ingested_at = datetime.now(timezone.utc)
    await session.commit()

    logger.info(
        "ingest_complete",
        symbol=symbol,
        bars=result.bars_written,
        movements=result.movements_detected,
        enriched=result.movements_enriched,
        articles=result.articles_linked,
        deferred=type(result.deferred).__name__ if result.deferred else None,
    )
    return result


@dataclass(slots=True)
class PriceRefresh:
    """What `refresh_prices` stored and found."""

    bars_written: int
    # Every movement detected in the stored history, after this refresh.
    movements: list[Movement]
    # Movements that did not exist before this refresh.
    created: list[Movement]
    # Movements whose news is not COMPLETE: new, PENDING, PARTIAL or FAILED.
    # Callers decide which of these to spend enrichment on (`needs_enrichment`).
    unfinished: list[Movement]


async def refresh_prices(
    session: AsyncSession, ticker: Ticker, history: price_service.PriceHistory
) -> PriceRefresh:
    """Store fetched bars, apply the profile, and re-run movement detection.

    `history` may be the full trailing year or only the last few weeks: it is
    merged with the bars already stored (rebasing them if a split or dividend
    has revised adjusted closes since), and detection runs over the merged
    series. Each day's rolling volatility is therefore computed from the full
    run of days before it, not just from what this fetch returned.

    Flushes but does not commit. Assumes the caller holds the ticker claim,
    since this rewrites the ticker's bars and may delete movements.
    """
    stored = [
        price_service.PriceBarData(
            date=bar.date,
            open=_as_float(bar.open),
            high=_as_float(bar.high),
            low=_as_float(bar.low),
            close=_as_float(bar.close),
            adj_close=float(bar.adj_close),
            volume=bar.volume,
        )
        for bar in (
            await session.scalars(
                sa.select(PriceBar).where(PriceBar.ticker_id == ticker.id)
            )
        ).all()
    ]
    merged = price_service.merge_price_bars(stored, history.bars)
    if merged.rebased_by is not None:
        logger.info(
            "price_history_rebased",
            symbol=ticker.symbol,
            factor=round(merged.rebased_by, 6),
        )
    if not merged.overlapped:
        # The fetch did not reach back to the stored bars, so a corporate
        # action in the gap would go unnoticed. The caller sizes the window to
        # avoid this; it is logged rather than fatal because the bars are real.
        logger.warning("price_history_gap", symbol=ticker.symbol)

    _apply_profile(ticker, history.profile)
    bars_written = await _upsert_price_bars(session, ticker, merged.bars)

    params = DetectionParams.from_settings()
    detected = detect_movements([bar.to_point() for bar in merged.bars], params)
    movements, created = await _upsert_movements(session, ticker, detected, params)
    return PriceRefresh(
        bars_written=bars_written,
        movements=movements,
        created=created,
        unfinished=[m for m in movements if m.news_status != NewsStatus.COMPLETE],
    )


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
        elif float(current.adj_close) != round(bar.adj_close, PRICE_DECIMALS):
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
) -> tuple[list[Movement], list[Movement]]:
    """Persist detected movements, preserving news already attached to them.

    Returns every detected movement, and the subset that is new.
    """
    existing: dict[object, Movement] = {
        m.date: m
        for m in (
            await session.scalars(
                sa.select(Movement).where(Movement.ticker_id == ticker.id)
            )
        ).all()
    }

    kept: list[Movement] = []
    created: list[Movement] = []
    for item in detected:
        movement = existing.get(item.date)
        if movement is None:
            movement = Movement(ticker_id=ticker.id, date=item.date)
            session.add(movement)
            created.append(movement)
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
        movement.news_window_closes_at = news_window_closes_at(item.date)
        kept.append(movement)

    # A day that no longer clears the threshold (revised prices, or retuned
    # parameters) stops being a movement, and its links go with it.
    detected_dates = {item.date for item in detected}
    for date_key, movement in existing.items():
        if date_key not in detected_dates:
            await session.delete(movement)

    await session.flush()
    return kept, created


async def _enrich_movement(
    session: AsyncSession,
    ticker: Ticker,
    movement: Movement,
    peers: PeerSet,
    news_provider: NewsProvider,
    llm: LLMProvider,
    result: IngestResult,
) -> int:
    """Search, score, and link news for one movement. Never raises.

    Safe to repeat: articles and links are upserted, and a successful scoring
    pass replaces whatever the previous pass linked.
    """
    movement.news_attempts += 1
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
        _record_failure(movement, result, exc, f"{movement.date}: news search: {exc}")
        return 0

    if not candidates:
        # No evidence is not counter-evidence: keep any links an earlier pass made.
        await _mark_enriched(session, movement)
        return 0

    try:
        scored = await score_candidates(context, candidates, peers, llm)
    except MetrixError as exc:
        _record_failure(movement, result, exc, f"{movement.date}: scoring: {exc}")
        return 0

    window_start, window_end = search_window(movement.date)
    linked = 0
    kept_articles: set[int] = set()
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

        kept_articles.add(article.id)
        if await _link(session, movement, article, item):
            linked += 1

    await _prune_links(session, movement, kept_articles)
    await _mark_enriched(session, movement)
    await session.flush()
    return linked


async def _mark_enriched(session: AsyncSession, movement: Movement) -> None:
    """COMPLETE if the news window had closed when this pass ran, else PARTIAL.

    A PARTIAL movement gets its follow-up enqueued here, in the same
    transaction as the status, so neither can exist without the other.
    """
    now = _now()
    closes_at = _as_utc(movement.news_window_closes_at)
    closed = now >= closes_at
    movement.news_status = NewsStatus.COMPLETE if closed else NewsStatus.PARTIAL
    movement.news_fetched_at = now
    if not closed:
        await queue.enqueue(
            session,
            JobKind.ENRICH_MOVEMENT,
            {"movement_id": movement.id},
            priority=queue.PRIORITY_FOLLOWUP,
            dedupe_key=queue.followup_key(movement.id),
            run_after=closes_at,
            source=JobSource.FOLLOWUP,
        )


async def _prune_links(
    session: AsyncSession, movement: Movement, kept_articles: set[int]
) -> None:
    """Drop links from an earlier pass that this pass's scorer did not support.

    On re-enrichment the scorer sees a fuller candidate set and ranks the old
    articles against new ones; an article it now scores below the threshold no
    longer explains the move as well as the alternatives, and keeping its old
    link would show a verdict the current pass contradicts.
    """
    await session.execute(
        sa.delete(MovementNewsLink)
        .where(
            MovementNewsLink.movement_id == movement.id,
            MovementNewsLink.article_id.not_in(kept_articles),
        )
        .execution_options(synchronize_session=False)
    )


def _record_failure(
    movement: Movement, result: IngestResult, error: MetrixError, message: str
) -> None:
    """Mark one movement's enrichment as failed without sinking the run."""
    logger.warning("movement_enrichment_failed", detail=message)
    movement.news_status = NewsStatus.FAILED
    result.warnings.append(message)
    result.failures.append(error)


async def _search_all_tiers(
    news_provider: NewsProvider, context: MovementContext, peers: PeerSet
) -> list[tuple[str, NewsCandidate]]:
    """Run the easy/medium/hard searches concurrently for one movement."""
    queries = build_tier_queries(context, peers)
    responses = await asyncio.gather(
        *(news_provider.search(request) for _, request in queries),
        return_exceptions=True,
    )

    # A search that had to wait (the spend cap, a busy provider) is not a
    # failed search: the tiers that ran are cached, and the movement is tried
    # again later from where it stands.
    for response in responses:
        if isinstance(response, WorkDeferred):
            raise response

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
        link.search_tier = item.search_tier
        link.scored_by = item.scored_by
        return False

    session.add(
        MovementNewsLink(
            movement_id=movement.id,
            article_id=article.id,
            relevance_tier=item.tier,
            relevance_score=item.score,
            rationale=item.rationale,
            search_tier=item.search_tier,
            scored_by=item.scored_by,
        )
    )
    await session.flush()
    return True


# ------------------------------------------------------------ queued work


async def enrich_movement(
    session: AsyncSession,
    movement_id: int,
    *,
    llm: LLMProvider | None = None,
    news_provider: NewsProvider | None = None,
    retry_exhausted: bool = False,
) -> IngestResult | None:
    """Enrich one movement by id, as a standalone, repeatable unit of work.

    Returns None without doing anything if the movement no longer exists (a
    price refresh re-ran detection and dropped it) or no longer needs
    enrichment (another job got there first). Unlike the per-movement step
    inside `ingest_ticker`, a failure here raises after it is recorded, so the
    job queue can retry it with backoff.
    """
    movement = await session.get(Movement, movement_id)
    if movement is None:
        logger.info("enrich_movement_gone", movement_id=movement_id)
        return None
    if not needs_enrichment(movement, _now(), retry_exhausted=retry_exhausted):
        logger.info(
            "enrich_movement_not_needed",
            movement_id=movement_id,
            news_status=movement.news_status.value,
        )
        return None

    ticker = await session.get(Ticker, movement.ticker_id)
    assert ticker is not None  # movements cascade-delete with their ticker
    llm = llm or get_llm_client()
    news_provider = news_provider or build_news_provider(session)
    result = IngestResult(symbol=ticker.symbol)

    peers = await resolve_peers(session, ticker, llm)
    result.articles_linked = await _enrich_movement(
        session, ticker, movement, peers, news_provider, llm, result
    )
    result.movements_enriched = 1
    await session.commit()

    if movement.news_status == NewsStatus.FAILED and result.failures:
        raise result.failures[-1]
    return result


async def release_claim(session: AsyncSession, symbol: str) -> None:
    """Give up the ingestion claim without recording a failure. Commits.

    For an ingestion that had to wait rather than failed: COMPLETE again if
    the ticker has data, PENDING if it never had any.
    """
    ticker = await get_ticker(session, symbol)
    if ticker is not None and ticker.ingest_status == IngestStatus.RUNNING:
        ticker.ingest_status = (
            IngestStatus.COMPLETE if ticker.last_ingested_at else IngestStatus.PENDING
        )
        await session.commit()


async def mark_ingestion_failed(
    session: AsyncSession, symbol: str, error: BaseException
) -> None:
    """Record a failed ingestion on the ticker and release its claim. Commits.

    Without this a crashed run leaves the ticker RUNNING until the claim goes
    stale, and callers see "ingesting" for that long instead of the error.
    """
    ticker = await get_ticker(session, symbol)
    if ticker is not None:
        ticker.ingest_status = IngestStatus.FAILED
        ticker.ingest_error = str(error)[:500]
        ticker.ingest_error_permanent = is_permanent(error)
        await session.commit()


async def _no_progress(_: dict[str, Any]) -> None:
    return None


def _as_float(value: object | None) -> float | None:
    """Stored prices are NUMERIC; the detector works in floats."""
    return None if value is None else float(value)  # type: ignore[arg-type]


def _now() -> datetime:
    """The clock freshness decisions are made against. A seam for tests."""
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres hands back aware ones."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
