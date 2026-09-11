"""Application configuration.

Every tunable lives here and is overridable by environment variable, so nothing
about movement detection or cost control is a hardcoded magic number.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "metrix"
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    # ------------------------------------------------------------------- db
    database_url: str = "postgresql+asyncpg://metrix:metrix@localhost:5432/metrix"

    # ------------------------------------------------------------------- llm
    llm_provider: Literal["anthropic"] = "anthropic"
    # Pinned by the assignment brief. Newer IDs (claude-sonnet-5, claude-opus-5)
    # are drop-in compatible with every call this app makes.
    llm_model: str = "claude-sonnet-4-6"
    llm_max_tokens: int = 4096
    llm_timeout_seconds: float = 60.0

    # Vendor credentials, named for their vendor like `exa_api_key` below. The
    # knobs above are not: every provider has a model, a token cap and a
    # timeout, so naming them after one vendor is what leaked in the first place.
    anthropic_api_key: str | None = None

    # ------------------------------------------------------------------ news
    news_provider: Literal["exa", "fixture"] = "exa"
    exa_api_key: str | None = None
    exa_base_url: str = "https://api.exa.ai"
    news_http_timeout_seconds: float = 30.0
    news_max_retries: int = 3

    # ------------------------------------------- movement detection (§ README)
    # A day is "major" when |return| >= max(floor, k * rolling_std_of_prior_N_days).
    movement_std_window: int = Field(
        default=20, ge=2, description="Trading days in the rolling volatility window."
    )
    movement_k: float = Field(
        default=2.0, gt=0, description="Multiplier applied to rolling stdev."
    )
    movement_floor_pct: float = Field(
        default=0.02,
        ge=0,
        description="Absolute floor on the threshold, as a decimal fraction (0.02 = 2%).",
    )

    # -------------------------------------------------------------- ingestion
    price_history_days: int = Field(
        default=365, ge=30, description="Default trailing window of prices to pull."
    )
    news_window_days_before: int = Field(
        default=3,
        ge=0,
        description="Days before the movement date to search for explanatory news.",
    )
    news_window_days_after: int = Field(
        default=1,
        ge=0,
        description="Days after the movement date to search (same-day follow-ups).",
    )
    max_movements_per_ingest: int = Field(
        default=10,
        ge=1,
        description=(
            "Cap on movements enriched with news per run, largest magnitude first. "
            "Bounds external API spend; re-running picks up the next batch."
        ),
    )
    max_candidates_per_tier: int = Field(
        default=8, ge=1, description="Candidate articles pulled per tier per movement."
    )
    relevance_min_score: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description="Articles scoring below this are not linked to the movement.",
    )
    staleness_hours: int = Field(
        default=24, ge=1, description="Age after which a ticker's data is refetched."
    )
    news_cache_ttl_hours: int = Field(
        default=24, ge=1, description="TTL for cached raw news-provider responses."
    )
    peers_ttl_days: int = Field(
        default=30, ge=1, description="TTL for the LLM-resolved competitor list."
    )

    # ------------------------------------------------------------------ chat
    chat_max_movements_in_context: int = Field(default=12, ge=1)
    chat_max_articles_per_movement: int = Field(default=4, ge=1)
    chat_max_history_messages: int = Field(default=20, ge=2)
    chat_max_movement_summaries: int = Field(
        default=60,
        ge=1,
        description="One-line movement summaries placed in the prompt. Cheap "
        "enough to include every movement, so date-specific questions can be "
        "answered even when that day is not among the most significant.",
    )

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            # Accept the canonical libpq URL and upgrade it, rather than failing
            # on the most common copy-paste mistake.
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
