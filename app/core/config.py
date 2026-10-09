"""Application configuration.

Every tunable lives here and is overridable by environment variable, so nothing
about movement detection or cost control is a hardcoded magic number.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Calendar days convert to trading days at roughly 5 in 7 (ignoring holidays).
TRADING_DAYS_PER_WEEK = 5


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
    price_batch_size: int = Field(
        default=100,
        ge=1,
        description="Symbols per yfinance batch download.",
    )
    prewarm_price_lookback_days: int = Field(
        default=45,
        ge=1,
        description=(
            "Calendar days of prices re-fetched for a ticker that already has "
            "stored bars; merged with them before detection. Must cover more "
            "trading days than MOVEMENT_STD_WINDOW."
        ),
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
    news_window_grace_hours: int = Field(
        default=6,
        ge=0,
        description=(
            "Hours after the news window's last day ends before it counts as "
            "closed. Covers late-indexed articles and provider crawl lag."
        ),
    )
    news_max_attempts: int = Field(
        default=3,
        ge=1,
        description=(
            "Enrichment attempts after which a FAILED movement stops being retried "
            "automatically. `refresh=true` still retries it."
        ),
    )
    news_cache_closed_window_ttl_days: int = Field(
        default=90,
        ge=1,
        description="TTL for cached news responses whose date window has closed.",
    )
    news_cache_open_window_ttl_hours: int = Field(
        default=2,
        ge=1,
        description=(
            "TTL for cached news responses whose date window is still open, "
            "so articles published later in the window are picked up."
        ),
    )
    peers_ttl_days: int = Field(
        default=30, ge=1, description="TTL for the LLM-resolved competitor list."
    )

    # ------------------------------------------------- demand and universe
    demand_half_life_days: float = Field(
        default=7.0,
        gt=0,
        description="Half-life of a ticker's popularity: a request counts half "
        "as much this many days later.",
    )
    prewarm_seed_symbols: str = Field(
        default="",
        description="Comma-separated symbols always pre-warmed, demand or not.",
    )
    prewarm_seed_file: str = Field(
        default="data/seed_universe.txt",
        description="Optional file of seed symbols, one per line; '#' starts a "
        "comment. Missing is fine.",
    )
    prewarm_top_n: int = Field(
        default=200,
        ge=0,
        description="Most popular tickers (by decayed popularity) pre-warmed nightly.",
    )
    prewarm_recent_days: int = Field(
        default=14,
        ge=0,
        description="Anything requested within this many days is pre-warmed too.",
    )

    # ---------------------------------------------------- nightly pre-warm
    prewarm_schedule_enabled: bool = Field(
        default=True,
        description="Whether worker processes schedule the nightly run. Safe to "
        "leave on in every replica: a trading date is only ever run once.",
    )
    prewarm_run_at: str = Field(
        default="17:15",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="Local time (PREWARM_TIMEZONE) of the nightly run, Mon-Fri. "
        "After the 16:00 close, once yfinance has settled the day's bars.",
    )
    prewarm_timezone: str = Field(
        default="America/New_York", description="Time zone of PREWARM_RUN_AT."
    )
    prewarm_jitter_seconds: float = Field(
        default=60.0,
        ge=0,
        description="Scheduled jobs start up to this many seconds either side of "
        "their nominal time, so a run does not stampede at one instant.",
    )
    prewarm_max_enrichments_per_run: int = Field(
        default=300,
        ge=0,
        description="Movement enrichments (news searches + one LLM call each) a "
        "nightly run may spend. The rest wait for demand or the next night.",
    )
    admin_token: str | None = Field(
        default=None,
        description="Required in the X-Admin-Token header by /admin endpoints, "
        "which are disabled while it is unset.",
    )

    # -------------------------------------------------------- rate limits
    # Per process. With N worker processes, set each to (provider limit) / N.
    exa_max_rps: float = Field(
        default=5.0, gt=0, description="Exa requests per second, per process."
    )
    anthropic_max_rpm: float = Field(
        default=50.0, gt=0, description="Anthropic requests per minute, per process."
    )
    yfinance_max_rps: float = Field(
        default=2.0, gt=0, description="yfinance requests per second, per process."
    )

    # ---------------------------------------------------------- job queue
    job_max_attempts: int = Field(
        default=3,
        ge=1,
        description="Default attempts per job before it is marked dead.",
    )
    job_retry_base_seconds: float = Field(
        default=30.0,
        gt=0,
        description="First retry delay. Doubles per attempt, with jitter.",
    )
    job_retry_max_seconds: float = Field(
        default=1800.0, gt=0, description="Ceiling on a single retry delay."
    )
    job_lock_timeout_minutes: float = Field(
        default=10.0,
        gt=0,
        description=(
            "A running job whose lock has not been refreshed for this long is "
            "assumed orphaned (its worker died) and is requeued."
        ),
    )
    job_heartbeat_seconds: float = Field(
        default=30.0,
        gt=0,
        description="How often a worker refreshes the lock on a job it is running.",
    )
    job_reap_interval_seconds: float = Field(
        default=60.0, gt=0, description="How often a worker looks for orphaned jobs."
    )
    job_error_max_chars: int = Field(
        default=2000, ge=100, description="`last_error` is truncated to this length."
    )

    # ------------------------------------------------------------- worker
    worker_concurrency: int = Field(
        default=4, ge=1, description="Concurrent claim loops in one worker process."
    )
    worker_interactive_slots: int = Field(
        default=1,
        ge=0,
        description=(
            "How many of those loops only claim priority-0 (a user is waiting) "
            "jobs, so a cold ticker never queues behind nightly work."
        ),
    )
    worker_poll_interval_seconds: float = Field(
        default=2.0, gt=0, description="Sleep between claims when the queue is empty."
    )
    worker_poll_jitter_seconds: float = Field(
        default=1.0,
        ge=0,
        description="Random extra sleep, so idle loops do not poll in lockstep.",
    )
    worker_shutdown_timeout_seconds: float = Field(
        default=30.0,
        ge=0,
        description=(
            "On SIGTERM, how long in-flight jobs may run before they are "
            "released back to the queue."
        ),
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

    @model_validator(mode="after")
    def _check_worker_settings(self) -> "Settings":
        if self.worker_interactive_slots >= self.worker_concurrency:
            raise ValueError(
                "WORKER_INTERACTIVE_SLOTS must be smaller than WORKER_CONCURRENCY, "
                "or nothing but interactive jobs would ever run."
            )
        trading_days = self.prewarm_price_lookback_days * TRADING_DAYS_PER_WEEK / 7
        if trading_days <= self.movement_std_window:
            raise ValueError(
                "PREWARM_PRICE_LOOKBACK_DAYS must span more trading days than "
                "MOVEMENT_STD_WINDOW, so the refreshed window overlaps the stored "
                "bars the newest days' rolling volatility is computed from."
            )
        if self.job_heartbeat_seconds >= self.job_lock_timeout_minutes * 60:
            raise ValueError(
                "JOB_HEARTBEAT_SECONDS must be shorter than JOB_LOCK_TIMEOUT_MINUTES, "
                "or healthy jobs would be reaped between heartbeats."
            )
        return self

    @field_validator("prewarm_timezone")
    @classmethod
    def _require_known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"PREWARM_TIMEZONE '{v}' is not a known IANA time zone") from exc
        return v

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
