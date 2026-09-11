"""Exa AI news search.

Why Exa over a keyword news API: the Medium and Hard tiers ask for articles
that explain a move *without* naming the company -- a Fed decision, a
competitor's guidance cut, a chip export rule. Keyword search over the ticker
finds none of those. Exa's neural search matches on meaning, and it supports a
published-date range, which is what pins an article to a specific movement.

Exa returns results in relevance order but exposes no numeric score, so
ordering is all the signal it gives. Deciding whether a result genuinely
explains a given movement is left to the LLM scoring pass in
`app.services.relevance`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.errors import ConfigurationError, NewsProviderError
from app.core.logging import get_logger
from app.services.news.base import (
    NewsCandidate,
    NewsProvider,
    NewsSearchRequest,
    parse_published_date,
    source_domain,
)

logger = get_logger(__name__)

# 429 and 5xx are worth another attempt; 4xx means the request itself is wrong
# and retrying just burns quota.
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class _RetryableUpstream(Exception):
    """Internal marker so tenacity retries transport errors and 5xx only."""


class ExaNewsProvider(NewsProvider):
    name = "exa"

    def __init__(self, api_key: str | None = None, client: httpx.AsyncClient | None = None):
        self._api_key = api_key or settings.exa_api_key
        self._client = client
        self._owns_client = client is None

    def _require_client(self) -> httpx.AsyncClient:
        if not self._api_key:
            raise ConfigurationError(
                "EXA_API_KEY is not set. Set it in .env, or set NEWS_PROVIDER=fixture "
                "to run the pipeline without a news API key."
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=settings.exa_base_url,
                timeout=settings.news_http_timeout_seconds,
                headers={
                    "x-api-key": self._api_key,
                    "content-type": "application/json",
                },
            )
        return self._client

    def _body(self, request: NewsSearchRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": request.query,
            "type": "auto",
            "numResults": request.num_results,
            "startPublishedDate": _iso_z(request.start),
            "endPublishedDate": _iso_z(request.end),
            "contents": {
                "text": {"maxCharacters": request.max_characters},
            },
        }
        if request.category:
            body["category"] = request.category
        if request.include_domains:
            body["includeDomains"] = list(request.include_domains)
        if request.summary_query:
            # Ask Exa to summarize each result *against the movement question*,
            # so the scoring pass reads a focused paragraph instead of 2k
            # characters of boilerplate.
            body["contents"]["summary"] = {"query": request.summary_query}
        return body

    async def execute(self, request: NewsSearchRequest) -> dict[str, Any]:
        client = self._require_client()
        try:
            return await self._post(client, self._body(request))
        except NewsProviderError:
            raise
        except Exception as exc:
            raise NewsProviderError("exa", f"search failed: {exc}") from exc

    @retry(
        retry=retry_if_exception_type(_RetryableUpstream),
        stop=stop_after_attempt(settings.news_max_retries),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _post(self, client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await client.post("/search", json=body)
        except httpx.RequestError as exc:
            raise _RetryableUpstream(f"transport error: {exc}") from exc

        if response.status_code in _RETRYABLE_STATUS:
            raise _RetryableUpstream(f"HTTP {response.status_code}: {response.text[:200]}")
        if response.status_code == 401:
            raise NewsProviderError("exa", "rejected the API key (HTTP 401)")
        if response.status_code >= 400:
            raise NewsProviderError(
                "exa", f"HTTP {response.status_code}: {response.text[:200]}"
            )

        payload = response.json()
        cost = (payload.get("costDollars") or {}).get("total")
        logger.info(
            "exa_search",
            query=body["query"][:80],
            results=len(payload.get("results") or []),
            cost_usd=cost,
        )
        return payload

    def parse(self, raw: dict[str, Any]) -> list[NewsCandidate]:
        candidates: list[NewsCandidate] = []
        for item in raw.get("results") or []:
            url = item.get("url")
            if not url:
                continue
            candidates.append(
                NewsCandidate(
                    url=url,
                    title=item.get("title"),
                    published_at=parse_published_date(item.get("publishedDate")),
                    author=item.get("author"),
                    summary=item.get("summary"),
                    content=item.get("text"),
                    source_domain=source_domain(url),
                    provider=self.name,
                    raw=item,
                )
            )
        return candidates

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _iso_z(value: datetime) -> str:
    """Exa wants `2023-01-01T00:00:00.000Z`."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


