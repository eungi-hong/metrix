"""Postgres-backed cache of raw news-provider responses.

News search is the rate-limited, metered part of ingestion. Caching the raw
payload means re-running ingestion during development is free, and two tickers
in the same sector share their macro-tier searches outright.

How long an entry lives depends on whether its date window has closed. Once it
has (window end plus `NEWS_WINDOW_GRACE_HOURS`), the answer for that window no
longer changes and the entry is kept for `NEWS_CACHE_CLOSED_WINDOW_TTL_DAYS`.
While it is still open, articles are still being published into it, so the
entry is kept only for `NEWS_CACHE_OPEN_WINDOW_TTL_HOURS`, and never past the
moment the window closes -- the first search after closing must see the final
answer, not a provisional one cached an hour earlier.

The cache wraps any provider and implements the same interface, so nothing
downstream knows whether a result came from the network or from a table.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.news import NewsQueryCache
from app.services.news.base import (
    NewsCandidate,
    NewsProvider,
    NewsSearchRequest,
    window_closes_at,
)

logger = get_logger(__name__)


def cache_expiry(request: NewsSearchRequest, now: datetime) -> datetime:
    """When a response fetched `now` for `request` should stop being served."""
    closes_at = window_closes_at(request.end)
    if now >= closes_at:
        return now + timedelta(days=settings.news_cache_closed_window_ttl_days)
    return min(
        now + timedelta(hours=settings.news_cache_open_window_ttl_hours), closes_at
    )


class CachingNewsProvider(NewsProvider):
    """Decorator: read-through cache in front of a real provider."""

    def __init__(self, inner: NewsProvider, session: AsyncSession) -> None:
        self._inner = inner
        self._session = session
        self.name = inner.name
        # The tier searches for one movement run concurrently on a single
        # AsyncSession, which is not safe for concurrent use. This serializes
        # the database touches only -- the upstream HTTP call stays outside it,
        # so the searches still overlap.
        self._db_lock = asyncio.Lock()

    async def execute(self, request: NewsSearchRequest) -> dict[str, Any]:
        key = request.cache_fingerprint(self._inner.name)
        now = datetime.now(timezone.utc)

        async with self._db_lock:
            cached = await self._lookup(key)
            if cached is not None and _as_utc(cached.expires_at) > now:
                logger.debug(
                    "news_cache_hit", provider=self.name, query=request.query[:60]
                )
                return cached.response

        raw = await self._inner.execute(request)

        async with self._db_lock:
            await self._store(key, request, raw, cache_expiry(request, now))
        return raw

    async def _lookup(self, key: str) -> NewsQueryCache | None:
        return await self._session.scalar(
            sa.select(NewsQueryCache).where(NewsQueryCache.cache_key == key)
        )

    async def _store(
        self,
        key: str,
        request: NewsSearchRequest,
        raw: dict[str, Any],
        expires_at: datetime,
    ) -> None:
        existing = await self._lookup(key)
        if existing is not None:
            existing.response = raw
            existing.expires_at = expires_at
            # Flush rather than commit: the caller's unit of work owns the transaction.
            await self._session.flush()
            return

        row = NewsQueryCache(
            cache_key=key,
            provider=self._inner.name,
            query=request.query,
            params={
                "start": request.start.isoformat(),
                "end": request.end.isoformat(),
                "num_results": request.num_results,
                "category": request.category,
                "include_domains": list(request.include_domains),
            },
            response=raw,
            expires_at=expires_at,
        )
        # Two workers that miss on the same key at the same moment both reach
        # this insert, and the second violates the unique constraint. That is
        # a cache hit that arrived late, not an error: the other worker stored
        # an equally good answer. The savepoint confines the failed INSERT so
        # the caller's transaction, and everything else it has written, survives.
        try:
            async with self._session.begin_nested():
                self._session.add(row)
        except IntegrityError:
            logger.info("news_cache_store_race", provider=self.name, cache_key=key)

    def parse(self, raw: dict[str, Any]) -> list[NewsCandidate]:
        return self._inner.parse(raw)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres hands back aware ones."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
