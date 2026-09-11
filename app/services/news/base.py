"""The news-provider seam.

Providers are split into `execute` (talk to the network, return the raw
payload) and `parse` (turn that payload into `NewsCandidate`s). Keeping them
apart is what lets `CachingNewsProvider` store the untouched upstream response
in Postgres and still re-derive typed candidates from it later -- including
after the parsing code has changed.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class NewsSearchRequest:
    """A single provider query. Hashable, and the cache key is derived from it."""

    query: str
    start: datetime
    end: datetime
    num_results: int = 8
    category: str | None = "news"
    include_domains: tuple[str, ...] = ()
    summary_query: str | None = None
    max_characters: int = 2000

    def cache_fingerprint(self, provider: str) -> str:
        payload = asdict(self) | {
            "provider": provider,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class NewsCandidate:
    """One article, normalized across providers."""

    url: str
    title: str | None = None
    published_at: datetime | None = None
    author: str | None = None
    summary: str | None = None
    content: str | None = None
    source_domain: str | None = None
    provider: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)

    def snippet(self, limit: int = 1200) -> str:
        """The text handed to the relevance model. Summary first, then body."""
        body = (self.summary or "").strip() or (self.content or "").strip()
        return body[:limit]


class NewsProvider(ABC):
    """Base class for news search backends."""

    name: str = "abstract"

    @abstractmethod
    async def execute(self, request: NewsSearchRequest) -> dict[str, Any]:
        """Perform the search and return the raw upstream JSON."""

    @abstractmethod
    def parse(self, raw: dict[str, Any]) -> list[NewsCandidate]:
        """Turn a raw upstream payload into candidates."""

    async def search(self, request: NewsSearchRequest) -> list[NewsCandidate]:
        return self.enforce_window(request, self.parse(await self.execute(request)))

    @staticmethod
    def enforce_window(
        request: NewsSearchRequest, candidates: list[NewsCandidate]
    ) -> list[NewsCandidate]:
        """Drop candidates published outside the requested window.

        Providers treat a published-date filter as a hint: Exa indexes a
        crawl date when an article exposes no publication metadata, and it
        does leak results well outside the range. Observed in practice: a
        search for the three days around a 2026-07-02 move returned an
        article published 2026-07-30, which the scoring model then rated 0.95
        as the explanation for a move four weeks earlier.

        The window is a hard requirement of the product, not a preference, so
        it is enforced here instead of being left to the provider or to the
        model's judgement. Articles with no publication date are kept -- the
        scorer is told the date is unknown and can discount them.
        """
        return [
            candidate
            for candidate in candidates
            if within_window(candidate.published_at, request.start, request.end)
        ]

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


def within_window(
    published_at: datetime | None, start: datetime, end: datetime
) -> bool:
    """Whether an article falls in a movement's news window.

    An unknown publication date passes: dropping those loses too much, and the
    scoring model is shown "unknown" and can discount them itself.
    """
    if published_at is None:
        return True
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    return start <= published_at <= end


def parse_published_date(value: Any) -> datetime | None:
    """Parse a provider's published date. Returns None rather than raising:
    a missing or malformed date must not cost us the article."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def source_domain(url: str) -> str | None:
    """The publisher's bare domain, used for display and source diversity."""
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else (host or None)
