"""Domain exception -> HTTP status mapping.

Registered once on the app so route handlers stay free of try/except noise and
every error response has the same shape (`ErrorOut`).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import (
    ConfigurationError,
    LLMError,
    MetrixError,
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

    @app.exception_handler(MetrixError)
    async def _domain_error(request: Request, exc: MetrixError) -> JSONResponse:
        logger.error("domain_error", detail=str(exc))
        return JSONResponse(status_code=500, content=_body("internal_error", str(exc)))
