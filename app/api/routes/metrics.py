"""GET /metrics in Prometheus text format.

Off unless METRICS_TOKEN is set (404), and then only with that token as a
bearer token or X-Metrics-Token, because it shows queue health and spend.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response

from app.api.deps import SessionDep
from app.core import metrics
from app.core.config import settings

router = APIRouter(tags=["health"])


def _token_ok(authorization: str | None, x_metrics_token: str | None) -> bool:
    expected = settings.metrics_token
    supplied = x_metrics_token
    if supplied is None and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    return bool(expected) and supplied is not None and secrets.compare_digest(supplied, expected)


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_metrics_token: Annotated[str | None, Header()] = None,
) -> Response:
    if not settings.metrics_token:
        raise HTTPException(status_code=404, detail="Not Found")
    if not _token_ok(authorization, x_metrics_token):
        raise HTTPException(status_code=401, detail="Missing or wrong metrics token.")
    await metrics.refresh_shared(session)
    return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)
