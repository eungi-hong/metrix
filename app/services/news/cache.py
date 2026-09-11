"""Postgres-backed cache of raw news-provider responses.

News search is the rate-limited, metered part of ingestion, and its answer for
a *past* date window never changes. Caching the raw payload means re-running
ingestion during development is free, and two tickers in the same sector share
their macro-tier searches outright.

The cache wraps any provider and implements the same interface, so nothing
downstream knows whether a result came from the network or from a table.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.models.news import NewsQueryCache
from app.services.news.base import NewsCandidate, NewsProvider, NewsSearchRequest

logger = get_logger(__name__)


class CachingNewsProvider(NewsProvider):
    """Decorator: read-through cache in front of a real provider."""

    def __init__(
        self,
        inner: NewsProvider,
        session: AsyncSession,
        ttl_hours: int | None = None,
    ) -> None:
        self._inner = inner
        self._session = session
        self._ttl = timedelta(hours=ttl_hours or settings.news_cache_ttl_hours)
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
            cached = await self._session.scalar(
                sa.select(NewsQueryCache).where(NewsQueryCache.cache_key == key)
            )
            if cached is not None and _as_utc(cached.expires_at) > now:
                logger.debug(
                    "news_cache_hit", provider=self.name, query=request.query[:60]
                )
                return cached.response

        raw = await self._inner.execute(request)

        async with self._db_lock:
            await self._store(key, request, raw, now + self._ttl)
        return raw

    async def _store(
        self,
        key: str,
        request: NewsSearchRequest,
        raw: dict[str, Any],
        expires_at: datetime,
    ) -> None:
        existing = await self._session.scalar(
            sa.select(NewsQueryCache).where(NewsQueryCache.cache_key == key)
        )
        if existing is not None:
            existing.response = raw
            existing.expires_at = expires_at
        else:
            self._session.add(
                NewsQueryCache(
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
            )
        # Flush rather than commit: the caller's unit of work owns the transaction.
        await self._session.flush()

    def parse(self, raw: dict[str, Any]) -> list[NewsCandidate]:
        return self._inner.parse(raw)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; Postgres hands back aware ones."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
