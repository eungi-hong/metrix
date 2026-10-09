"""The process's Redis client: created on first use, closed on shutdown.

Redis holds nothing durable here, only short-lived counters for limits, and
the service runs without it (see `app.services.limits`). So the client is
lazy, has short timeouts, and `ping` reports instead of raising: a Redis
outage is "degraded", never "down".
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis | None:
    """The shared client, or None when REDIS_URL is unset."""
    global _client
    if settings.redis_url is None:
        return None
    if _client is None:
        _client = aioredis.from_url(
            settings.redis_url,
            socket_timeout=settings.redis_timeout_seconds,
            socket_connect_timeout=settings.redis_timeout_seconds,
            # A dead connection is replaced, not retried in a loop: the
            # limiter's fallback handles the failure.
            retry_on_timeout=False,
            health_check_interval=30,
        )
    return _client


async def ping() -> str:
    """"ok", "unreachable", or "not_configured"."""
    client = get_redis()
    if client is None:
        return "not_configured"
    try:
        await client.ping()
    except Exception:
        return "unreachable"
    return "ok"


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
