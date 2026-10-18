"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_exception_handlers
from app.api.middleware import InteractiveAttributionMiddleware, RequestContextMiddleware
from app.api.routes import admin, chat, conversations, events, health, jobs, metrics, research, tickers, watchlists
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import close_redis
from app.db.session import dispose_engine
from app.services import events as event_streams
from app.services import spend

logger = get_logger(__name__)

DESCRIPTION = """\
Explains major stock price movements using the news that caused them.

A movement is a day where `|return| >= max(floor, k * rolling_std)` -- a flat
floor so quiet stocks are not flagged on noise, and a volatility term so the
bar adapts to each stock's own regime.

News is searched in three tiers and then scored by an LLM for whether it
actually explains the move: **easy** (company-specific), **medium**
(competitor / industry) and **hard** (macro, political, regulatory).
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    logger.info(
        "startup",
        env=settings.app_env,
        news_provider=settings.news_provider,
        llm_provider=settings.llm_provider,
        model=settings.llm_model,
    )
    spend.warn_on_startup()
    yield
    await event_streams.close_broker()
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Metrix -- Stock Movement News Explainer",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
    )
    # The API remains same-origin by default. Local browser clients opt in via
    # CORS_ALLOWED_ORIGINS; this is intentionally a narrow allow-list rather
    # than a permissive development wildcard. CORS_ALLOWED_ORIGIN_REGEX adds
    # preview deployments, whose URLs change on every push.
    cors_origins = [origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()]
    cors_origin_regex = settings.cors_allowed_origin_regex or None
    if cors_origins or cors_origin_regex:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_origin_regex=cors_origin_regex,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-Exa-Api-Key",
                "X-OpenAI-Api-Key",
                "X-Request-ID",
            ],
            expose_headers=["ETag", "X-Request-ID"],
            max_age=600,
        )
    app.add_middleware(InteractiveAttributionMiddleware)
    # Outermost, so the id and the latency cover everything, CORS included.
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(tickers.router)
    app.include_router(research.router)
    app.include_router(watchlists.router)
    app.include_router(chat.router)
    app.include_router(conversations.router)
    app.include_router(jobs.router)
    app.include_router(events.router)
    app.include_router(admin.router)
    app.include_router(metrics.router)
    return app


app = create_app()
