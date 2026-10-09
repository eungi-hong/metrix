"""The job queue and the nightly pre-warm runs that feed it.

The queue lives in Postgres rather than Redis or a broker. It is one more
table, not one more service, and a job can be enqueued in the same transaction
as the data that caused it: a PARTIAL enrichment and its follow-up job commit
together or not at all. The cost is throughput, which is irrelevant here --
the bottleneck is external API rate limits, at a few jobs per second at most.
`app.services.queue` holds the semantics; this module holds the shape.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType
from app.models.enums import sa_enum


class JobKind(StrEnum):
    INGEST_TICKER = "ingest_ticker"
    REFRESH_PRICES = "refresh_prices"
    PREWARM_SECTOR_MACRO = "prewarm_sector_macro"
    ENRICH_MOVEMENT = "enrich_movement"
    SCHEDULE_NIGHTLY = "schedule_nightly"


class JobStatus(StrEnum):
    """QUEUED and RUNNING are active; the rest are terminal.

    A failed attempt that will be retried goes back to QUEUED with a later
    `run_after`, so "waiting to retry" needs no state of its own. DEAD means
    the queue gave up: a permanent error, or `max_attempts` used up. FAILED is
    terminal too, and is treated like DEAD wherever terminal states matter.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class JobSource(StrEnum):
    """Why a job exists. Kept for metrics: who is the queue working for?"""

    INTERACTIVE = "interactive"
    SCHEDULED = "scheduled"
    FOLLOWUP = "followup"


class PrewarmRunStatus(StrEnum):
    RUNNING = "running"
    FINISHED = "finished"


_ACTIVE_SQL = "status IN ('queued', 'running')"


class Job(Base):
    """One unit of background work.

    `dedupe_key` names the work, not the row: "ingest:NVDA", "enrich:42". At
    most one active job may hold a key, which the partial unique index enforces
    and `queue.enqueue` relies on to turn a duplicate request into a priority
    bump. Terminal jobs keep their key as history and do not block new ones.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        sa.Index("ix_jobs_claim", "status", "priority", "run_after"),
        sa.Index(
            "uq_jobs_active_dedupe_key",
            "dedupe_key",
            unique=True,
            postgresql_where=sa.text(_ACTIVE_SQL),
            sqlite_where=sa.text(_ACTIVE_SQL),
        ),
        sa.Index("ix_jobs_dedupe_key", "dedupe_key"),
        sa.Index("ix_jobs_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[JobKind] = mapped_column(sa_enum(JobKind, "job_kind", 32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    priority: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        sa_enum(JobStatus, "job_status"), default=JobStatus.QUEUED, nullable=False
    )
    dedupe_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # Not claimable while an active job holds this dedupe key. A blocker that
    # ends in any terminal state, including DEAD, releases its dependents.
    blocked_by_key: Mapped[str | None] = mapped_column(sa.String(255))
    run_after: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(sa.String(128))
    # Set at claim and refreshed by the worker's heartbeat while it runs.
    locked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)

    source: Mapped[JobSource] = mapped_column(sa_enum(JobSource, "job_source"), nullable=False)
    progress: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    run_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("prewarm_runs.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_JOB_STATUSES

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Job {self.id} {self.kind} {self.dedupe_key} {self.status}>"


class PrewarmRun(Base):
    """One nightly pre-warm pass over the universe.

    The enrichment budget is enforced against this row with a conditional
    UPDATE (`queue.take_enrichment_budget`), so jobs from several price chunks
    running in parallel cannot overspend it between them.
    """

    __tablename__ = "prewarm_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trading_date: Mapped[date] = mapped_column(sa.Date, unique=True, nullable=False)
    status: Mapped[PrewarmRunStatus] = mapped_column(
        sa_enum(PrewarmRunStatus, "prewarm_run_status"),
        default=PrewarmRunStatus.RUNNING,
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    universe_size: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    movements_found: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    enrichments_queued: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    enrichments_deferred: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    enrichment_budget: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    enrichments_used: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
