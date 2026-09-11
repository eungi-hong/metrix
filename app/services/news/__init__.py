"""News search providers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.news.base import NewsCandidate, NewsProvider, NewsSearchRequest
from app.services.news.cache import CachingNewsProvider
from app.services.news.exa import ExaNewsProvider
from app.services.news.fixture import FixtureNewsProvider

_PROVIDERS: dict[str, type[NewsProvider]] = {
    "exa": ExaNewsProvider,
    "fixture": FixtureNewsProvider,
}


def build_news_provider(
    session: AsyncSession, provider_name: str | None = None
) -> NewsProvider:
    """The configured provider, wrapped in the read-through response cache."""
    name = (provider_name or settings.news_provider).lower()
    try:
        provider_cls = _PROVIDERS[name]
    except KeyError:  # pragma: no cover - guarded by pydantic Literal
        raise ValueError(f"Unknown news provider '{name}'") from None
    return CachingNewsProvider(provider_cls(), session)


__all__ = [
    "CachingNewsProvider",
    "ExaNewsProvider",
    "FixtureNewsProvider",
    "NewsCandidate",
    "NewsProvider",
    "NewsSearchRequest",
    "build_news_provider",
]
