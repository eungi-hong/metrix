"""HTTP validation and a shared response cache for GET /tickers/{symbol}.

The ETag is a hash of everything the response is a function of:

* the ticker's `data_version` (and its benchmarks' when they are included),
  which every committed write that changes the response increments
  (`app.db.versions`);
* the query parameters, normalised (sorted, repeated values kept);
* what this request's freshness check decided (state, message, job id,
  warnings), which can differ between two requests for the same data;
* the response schema, so a deploy that changes the shape changes every tag.

So the tag can be computed, and a `304 Not Modified` answered, without
building the response. The same tag keys the shared Redis cache, which is
invalidated by construction: once the worker commits a change, the version
moves and no request asks for the old key again; the old entry expires.

Redis being down makes this a no-op (every lookup misses), never an error.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core import metrics
from app.core.config import settings
from app.core.logging import get_logger
from app.core.redis import get_redis
from app.schemas.market import TickerDetailOut

logger = get_logger(__name__)

KEY_PREFIX = "metrix:resp:ticker:"
SCHEMA_FINGERPRINT = hashlib.sha256(
    json.dumps(TickerDetailOut.model_json_schema(), sort_keys=True).encode()
).hexdigest()[:16]


def etag_for(parts: dict[str, Any]) -> str:
    """A strong ETag (quoted) for these inputs."""
    payload = json.dumps({"schema": SCHEMA_FINGERPRINT, **parts}, sort_keys=True, default=str)
    return '"' + hashlib.sha256(payload.encode()).hexdigest()[:32] + '"'


def matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match: `*`, or any listed tag equal under weak comparison."""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True
    bare = etag.removeprefix("W/")
    return any(tag.strip().removeprefix("W/") == bare for tag in if_none_match.split(","))


def cache_control(state: str) -> str:
    """Fresh for a minute and servable stale while revalidating, for settled
    data; must revalidate while a refresh is running."""
    if state == "ready":
        return (
            f"private, max-age={settings.http_cache_max_age_seconds}, "
            f"stale-while-revalidate={settings.http_cache_stale_seconds}"
        )
    return "private, no-cache"


async def get(etag: str) -> bytes | None:
    if not settings.response_cache_enabled:
        metrics.CACHE_REQUESTS.labels("ticker", "bypass").inc()
        return None
    client = get_redis()
    if client is None:
        metrics.CACHE_REQUESTS.labels("ticker", "bypass").inc()
        return None
    try:
        body = await client.get(KEY_PREFIX + etag.strip('"'))
    except Exception as exc:  # Redis trouble is a miss, never an error
        logger.debug("response_cache_unavailable", error=str(exc))
        metrics.CACHE_REQUESTS.labels("ticker", "bypass").inc()
        return None
    metrics.CACHE_REQUESTS.labels("ticker", "hit" if body is not None else "miss").inc()
    return body


async def put(etag: str, body: bytes) -> None:
    if not settings.response_cache_enabled or len(body) > settings.response_cache_max_bytes:
        return
    client = get_redis()
    if client is None:
        return
    try:
        await client.set(KEY_PREFIX + etag.strip('"'), body, ex=settings.response_cache_ttl_seconds)
    except Exception as exc:
        logger.debug("response_cache_unavailable", error=str(exc))
