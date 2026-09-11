"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import register_exception_handlers
from app.api.routes import chat, health, tickers
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.session import dispose_engine

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
        model=settings.anthropic_model,
    )
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Metrix -- Stock Movement News Explainer",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(tickers.router)
    app.include_router(chat.router)
    return app


app = create_app()
