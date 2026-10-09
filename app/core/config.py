"""Application configuration.

Every tunable lives here and is overridable by environment variable, so nothing
about movement detection or cost control is a hardcoded magic number.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Calendar days convert to trading days at roughly 5 in 7 (ignoring holidays).
TRADING_DAYS_PER_WEEK = 5


class LLMPrice(BaseModel):
    """One model's prices, in US dollars per million tokens."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cache_read_per_mtok: float = Field(ge=0)
    cache_write_per_mtok: float = Field(ge=0)


class PlanLimits(BaseModel):
    """What one plan may do. Every count is per principal: a user, or an
    anonymous caller's IP address."""

    requests_per_minute: int = Field(ge=0, description="Every authenticated API call.")
    chat_per_minute: int = Field(ge=0)
    chat_per_day: int = Field(ge=0)
    cold_ingests_per_day: int = Field(
        ge=0, description="Requests that start new ingestion or news work; a warm ticker never counts."
    )
    refresh_per_day: int = Field(ge=0, description="refresh=true requests that cause work.")
    allow_wait: bool = Field(description="Whether wait=true may run work inline in the API.")


# Starting points, meant to be tuned. Anonymous callers (AUTH_REQUIRED=false)
# get the least, and cannot force refreshes or run work inline.
DEFAULT_PLAN_LIMITS: dict[str, PlanLimits] = {
    "anonymous": PlanLimits(
        requests_per_minute=30, chat_per_minute=2, chat_per_day=10,
        cold_ingests_per_day=3, refresh_per_day=0, allow_wait=False,
    ),
    "free": PlanLimits(
        requests_per_minute=60, chat_per_minute=5, chat_per_day=50,
        cold_ingests_per_day=10, refresh_per_day=5, allow_wait=False,
    ),
    "pro": PlanLimits(
        requests_per_minute=300, chat_per_minute=20, chat_per_day=500,
        cold_ingests_per_day=100, refresh_per_day=50, allow_wait=True,
    ),
    "internal": PlanLimits(
        requests_per_minute=1200, chat_per_minute=60, chat_per_day=5000,
        cold_ingests_per_day=1000, refresh_per_day=500, allow_wait=True,
    ),
}


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
    # ------------------------------------------------------------ identity
    auth_required: bool = Field(
        default=True,
        description="Require an API key (Authorization: Bearer mtx_...) on /tickers, "
        "/chat, /jobs and /conversations. When false, callers without one are "
        "served as an anonymous principal, keyed by client IP, on the "
        "restrictive anonymous plan. A key that is sent is always checked.",
    )
    trusted_proxy_count: int = Field(
        default=0,
        ge=0,
        description="How many reverse proxies in front of the API append to "
        "X-Forwarded-For. 0 (the default) ignores the header and uses the socket "
        "peer, since a client can write anything into it.",
    )
    api_key_touch_interval_minutes: float = Field(
        default=5.0,
        ge=0,
        description="A key's last_used_at is refreshed at most this often, so "
        "authenticating is not a write on every request.",
    )
    plan_limits_json: Annotated[dict[str, PlanLimits], NoDecode] = Field(
        default_factory=lambda: dict(DEFAULT_PLAN_LIMITS),
        description="JSON map of plan -> limits, overriding the defaults field by "
        "field, e.g. {\"free\": {\"chat_per_day\": 20}}. Plans: anonymous, free, "
        "pro, internal.",
    )
    api_max_inline_ingestions: int = Field(
        default=2,
        ge=0,
        description="wait=true requests running work inline at once, per API "
        "process. Past this they get 429, so inline work can never take over "
        "the API's capacity.",
    )

    # -------------------------------------------------------------- redis
    redis_url: str | None = Field(
        default=None,
        description="Redis for per-user limits (and, later, shared provider rate "
        "limits). Unset: per-process in-memory limits, with a warning.",
    )
    redis_timeout_seconds: float = Field(
        default=0.25,
        gt=0,
        description="Connect and command timeout. Short: a limit check is on every "
        "request, and a slow Redis must fall back rather than slow the API.",
    )
    redis_retry_seconds: float = Field(
        default=5.0,
        gt=0,
        description="After Redis fails, use the in-memory fallback for this long "
        "before trying Redis again, so an outage costs one timeout, not one per request.",
    )
    limiter_fallback_log_seconds: float = Field(
        default=60.0,
        gt=0,
        description="limiter_fallback is logged at most this often while Redis is down.",
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

    # -------------------------------------------------------------- spend
    # Prices are configuration, never code: they change, and a guessed price
    # in a cap is worse than none. See .env.example for the shape.
    llm_prices_json: Annotated[dict[str, LLMPrice], NoDecode] = Field(
        default_factory=dict,
        description="JSON map of model id -> {input_per_mtok, output_per_mtok, "
        "cache_read_per_mtok, cache_write_per_mtok}, in USD per million tokens. "
        "Calls to a model missing here are recorded at the fallback rate below "
        "and flagged cost_estimated.",
    )
    llm_fallback_input_per_mtok: float = Field(
        default=20.0,
        ge=0,
        description="Rate for input (and cache) tokens of a model with no entry in "
        "LLM_PRICES_JSON. Not a price: a deliberately pessimistic stand-in, so an "
        "unpriced model overcounts against the cap rather than slipping under it.",
    )
    llm_fallback_output_per_mtok: float = Field(
        default=100.0,
        ge=0,
        description="Rate for output tokens of an unpriced model. Pessimistic, as above.",
    )
    exa_cost_estimate_usd: float = Field(
        default=0.05,
        ge=0,
        description="Cost assumed for one Exa search: reserved against the cap "
        "before each search, and recorded (flagged estimated) when Exa's "
        "response carries no costDollars. Deliberately high; set it from your "
        "Exa usage page.",
    )
    daily_spend_cap_usd: float | None = Field(
        default=None,
        gt=0,
        description="Most the service may spend on Exa and the LLM per UTC day. "
        "Required when APP_ENV=prod; unset means unlimited (with a warning).",
    )
    background_spend_share: float = Field(
        default=0.6,
        ge=0,
        le=1,
        description="Fraction of the daily cap background work (the nightly run, "
        "follow-ups) may use. The rest is reserved for users.",
    )
    spend_alert_fraction: float = Field(
        default=0.8,
        gt=0,
        le=1,
        description="Log spend_threshold_crossed, once a day, when settled spend "
        "reaches this fraction of the cap.",
    )
    spend_resume_jitter_seconds: float = Field(
        default=300.0,
        ge=0,
        description="Jobs held by the cap resume within this many seconds after "
        "00:00 UTC, so they do not all start in the same second.",
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

    @field_validator("llm_prices_json", mode="before")
    @classmethod
    def _parse_prices(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        if not v.strip():
            return {}
        try:
            raw = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM_PRICES_JSON is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("LLM_PRICES_JSON must be a JSON object keyed by model id")
        prices = {}
        for model, entry in raw.items():
            try:
                prices[model] = LLMPrice.model_validate(entry)
            except ValidationError as exc:
                fields = ", ".join(str(e["loc"][0]) for e in exc.errors() if e["loc"])
                raise ValueError(
                    f"LLM_PRICES_JSON entry for '{model}' needs non-negative "
                    f"input_per_mtok, output_per_mtok, cache_read_per_mtok and "
                    f"cache_write_per_mtok (problem with: {fields or 'the entry'})"
                ) from exc
        return prices

    @field_validator("plan_limits_json", mode="before")
    @classmethod
    def _parse_plan_limits(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return v
        if not v.strip():
            return dict(DEFAULT_PLAN_LIMITS)
        try:
            raw = json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError(f"PLAN_LIMITS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("PLAN_LIMITS_JSON must be a JSON object keyed by plan")
        unknown = sorted(set(raw) - set(DEFAULT_PLAN_LIMITS))
        if unknown:
            raise ValueError(
                f"PLAN_LIMITS_JSON has unknown plan(s) {unknown}; "
                f"plans are {sorted(DEFAULT_PLAN_LIMITS)}"
            )
        limits = dict(DEFAULT_PLAN_LIMITS)
        for plan, overrides in raw.items():
            if not isinstance(overrides, dict):
                raise ValueError(f"PLAN_LIMITS_JSON entry for '{plan}' must be an object")
            try:
                limits[plan] = PlanLimits.model_validate(
                    DEFAULT_PLAN_LIMITS[plan].model_dump() | overrides
                )
            except ValidationError as exc:
                problems = "; ".join(
                    f"{e['loc'][0]}: {e['msg']}" for e in exc.errors() if e["loc"]
                )
                raise ValueError(f"PLAN_LIMITS_JSON entry for '{plan}': {problems}") from exc
        return limits

    @field_validator("daily_spend_cap_usd", mode="before")
    @classmethod
    def _blank_cap_is_unset(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @model_validator(mode="after")
    def _require_cap_in_prod(self) -> "Settings":
        if self.app_env == "prod" and self.daily_spend_cap_usd is None:
            raise ValueError(
                "DAILY_SPEND_CAP_USD is required when APP_ENV=prod: without it "
                "nothing bounds what the service can spend in a day."
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
