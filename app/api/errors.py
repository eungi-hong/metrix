"""Domain exception -> HTTP status mapping.

Registered once on the app so route handlers stay free of try/except noise and
every error response has the same shape (`ErrorOut`).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import (
    AuthenticationError,
    ConfigurationError,
    LLMError,
    MetrixError,
    RateLimited,
    SpendCapReached,
    TickerNotFoundError,
    UpstreamError,
)
from app.core.logging import get_logger
from app.schemas.chat import ErrorOut

logger = get_logger(__name__)


def _body(error: str, detail: str) -> dict[str, str]:
    # Built through the schema so the documented error shape and the one
    # actually returned cannot drift apart.
    return ErrorOut(error=error, detail=detail).model_dump()


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(TickerNotFoundError)
    async def _not_found(request: Request, exc: TickerNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content=_body("not_found", str(exc)))

    @app.exception_handler(AuthenticationError)
    async def _unauthenticated(request: Request, exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=_body("unauthorized", str(exc)),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(ConfigurationError)
    async def _misconfigured(
        request: Request, exc: ConfigurationError
    ) -> JSONResponse:
        # 503, not 500: the service is correct but not fully provisioned, and
        # the message says exactly which variable is missing.
        logger.error("configuration_error", detail=str(exc))
        return JSONResponse(
            status_code=503, content=_body("configuration_error", str(exc))
        )

    @app.exception_handler(LLMError)
    async def _llm_failed(request: Request, exc: LLMError) -> JSONResponse:
        logger.warning("llm_error", detail=str(exc))
        return JSONResponse(
            status_code=502,
            content=_body("upstream_unavailable", f"Language model unavailable: {exc}"),
        )

    @app.exception_handler(UpstreamError)
    async def _upstream_failed(request: Request, exc: UpstreamError) -> JSONResponse:
        logger.warning("upstream_error", provider=exc.provider, detail=str(exc))
        return JSONResponse(
            status_code=502, content=_body("upstream_unavailable", str(exc))
        )

    @app.exception_handler(RateLimited)
    async def _rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        headers = exc.result.headers()
        headers["Retry-After"] = str(max(math.ceil(exc.result.retry_after), 1))
        return JSONResponse(
            status_code=429, content=_body("rate_limited", str(exc)), headers=headers
        )

    @app.exception_handler(SpendCapReached)
    async def _spend_capped(request: Request, exc: SpendCapReached) -> JSONResponse:
        # 503 with Retry-After: the service is fine, it has spent today's
        # budget, and the client is told exactly when to come back.
        wait = math.ceil((exc.retry_at - datetime.now(timezone.utc)).total_seconds())
        return JSONResponse(
            status_code=503,
            content=_body("spend_cap_reached", str(exc)),
            headers={"Retry-After": str(max(wait, 1))},
        )

    @app.exception_handler(MetrixError)
    async def _domain_error(request: Request, exc: MetrixError) -> JSONResponse:
        logger.error("domain_error", detail=str(exc))
        return JSONResponse(status_code=500, content=_body("internal_error", str(exc)))
