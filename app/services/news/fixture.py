"""Deterministic offline news provider.

Exists so the full pipeline -- ingestion, relevance scoring, the API, the
tests -- can be exercised without an Exa key and without spending quota. It
synthesizes plausible headlines in the requested window and tags them with the
tier the query was aiming at. Enable with `NEWS_PROVIDER=fixture`.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from app.services.news.base import (
    NewsCandidate,
    NewsProvider,
    NewsSearchRequest,
    parse_published_date,
    source_domain,
)

_TEMPLATES = [
    ("{q} -- what analysts are saying", "marketwatch.example"),
    ("Breaking: {q}", "reuters.example"),
    ("{q}: five things to know", "bloomberg.example"),
    ("Investors weigh {q}", "ft.example"),
]


class FixtureNewsProvider(NewsProvider):
    name = "fixture"

    async def execute(self, request: NewsSearchRequest) -> dict[str, Any]:
        # Seeded on the query so repeated runs return byte-identical results.
        seed = int(hashlib.sha256(request.query.encode()).hexdigest()[:8], 16)
        span = max((request.end - request.start).days, 1)
        results = []
        for i in range(min(request.num_results, len(_TEMPLATES))):
            title_tpl, domain = _TEMPLATES[(seed + i) % len(_TEMPLATES)]
            published = request.start + timedelta(days=(seed + i) % span)
            slug = hashlib.sha256(f"{request.query}{i}".encode()).hexdigest()[:12]
            results.append(
                {
                    "id": slug,
                    "url": f"https://{domain}/articles/{slug}",
                    "title": title_tpl.format(q=request.query[:60]),
                    "publishedDate": published.isoformat(),
                    "author": "Fixture Wire",
                    "text": (
                        f"Synthetic fixture article generated for the query "
                        f"'{request.query}'. Published {published.date()}. "
                        "Used for offline development and tests; not real reporting."
                    ),
                }
            )
        return {"requestId": f"fixture-{seed}", "results": results}

    def parse(self, raw: dict[str, Any]) -> list[NewsCandidate]:
        return [
            NewsCandidate(
                url=item["url"],
                title=item.get("title"),
                published_at=parse_published_date(item.get("publishedDate")),
                author=item.get("author"),
                summary=None,
                content=item.get("text"),
                source_domain=source_domain(item["url"]),
                provider=self.name,
                raw=item,
            )
            for item in raw.get("results") or []
        ]
