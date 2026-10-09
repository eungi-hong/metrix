"""Response models for the admin endpoints."""

from __future__ import annotations

from datetime import date, datetime

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
    dead: list[DeadJob] = Field(description="The 20 most recent dead jobs.")
    last_run: PrewarmRunOut | None
