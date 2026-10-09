"""Response models for the admin endpoints."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.jobs import JobKind, JobStatus, PrewarmRunStatus


class PrewarmTriggered(BaseModel):
    job_id: int
    trading_date: date


class JobCount(BaseModel):
    kind: JobKind
    status: JobStatus
    count: int


class DeadJob(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: JobKind
    dedupe_key: str
    attempts: int
    last_error: str | None
    finished_at: datetime | None


class PrewarmRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    trading_date: date
    status: PrewarmRunStatus
    started_at: datetime
    finished_at: datetime | None
    universe_size: int
    movements_found: int
    enrichments_queued: int
    enrichments_used: int
    enrichments_deferred: int
    enrichment_budget: int
    duration_seconds: float | None = None


class QueueSummary(BaseModel):
    counts: list[JobCount] = Field(description="Jobs by (kind, status).")
    oldest_queued_age_seconds: float | None = Field(
        description="How long the longest-waiting due job has waited; the "
        "number that grows when workers cannot keep up."
    )
    held_by_spend_cap: int = Field(
        description="Queued jobs waiting for the daily spend cap to reset."
    )
    dead: list[DeadJob] = Field(description="The 20 most recent dead jobs.")
    last_run: PrewarmRunOut | None


class SpendByOperation(BaseModel):
    provider: str
    operation: str
    calls: int
    cost_usd: Decimal
    estimated_calls: int = Field(description="Calls whose cost is an estimate, not the provider's figure.")


class SpendByUser(BaseModel):
    user_id: int
    calls: int
    cost_usd: Decimal


class UsageSummary(BaseModel):
    """One UTC day's spend. Ledger figures are settled calls only; calls in
    flight show under `reserved_usd`."""

    date: date
    total_usd: Decimal
    background_usd: Decimal
    interactive_usd: Decimal
    calls: int
    by_operation: list[SpendByOperation]
    top_users: list[SpendByUser] = Field(description="The 10 users who spent most.")
    cap_usd: Decimal | None = Field(description="DAILY_SPEND_CAP_USD; null means unlimited.")
    background_cap_usd: Decimal | None
    reserved_usd: Decimal = Field(description="Set aside by calls still in flight.")
    headroom_usd: Decimal | None = Field(description="What interactive calls may still spend today.")
    background_headroom_usd: Decimal | None = Field(
        description="What background calls may still spend today."
    )
    interactive_capped: bool = Field(description="An interactive call has been refused today.")
    background_capped: bool = Field(description="A background call has been refused today.")
