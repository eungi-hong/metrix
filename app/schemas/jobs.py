"""Response model for the job endpoint."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.jobs import JobKind, JobSource, JobStatus


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: JobKind
    status: JobStatus = Field(
        description="'queued' (including waiting to retry), 'running', "
        "'succeeded', or 'dead' (gave up: a permanent error or too many attempts)."
    )
    source: JobSource
    priority: int = Field(description="Lower runs first. 0 means a user is waiting.")
    payload: dict[str, Any]
    progress: dict[str, Any] | None = Field(
        default=None, description="Handler-reported progress, e.g. done/total."
    )
    attempts: int
    max_attempts: int
    last_error: str | None = None
    run_after: datetime = Field(description="Not started before this instant.")
    created_at: datetime
    locked_at: datetime | None = Field(
        default=None, description="When a worker claimed it, or last heartbeated."
    )
    finished_at: datetime | None = None
