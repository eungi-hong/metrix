"""Liveness and readiness.

Postgres unreachable is "degraded" and the service can do little. Redis
unreachable is also "degraded", but everything keeps working: limits fall
back to per-process counters (`app.services.limits`).
"""

from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter

from app.api.deps import SessionDep
from app.core import redis
from app.core.config import settings
from app.services.llm import get_llm_client

router = APIRouter(tags=["health"])


@router.get("/health", summary="Service health and which integrations are configured")
async def health(session: SessionDep) -> dict[str, object]:
    try:
        await session.execute(sa.select(1))
        database_ok = True
    except Exception:
        database_ok = False

    redis_status = await redis.ping()
    return {
        "status": "ok" if database_ok and redis_status != "unreachable" else "degraded",
        "database": "ok" if database_ok else "unreachable",
        "redis": redis_status,
        "news_provider": settings.news_provider,
        "news_provider_configured": (
            settings.news_provider == "fixture" or bool(settings.exa_api_key)
        ),
        "llm_provider": settings.llm_provider,
        "llm_configured": get_llm_client().available,
        "movement_detection": {
            "window": settings.movement_std_window,
            "k": settings.movement_k,
            "floor_pct": settings.movement_floor_pct,
        },
    }
