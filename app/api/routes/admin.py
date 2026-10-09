"""Operator endpoints: trigger a pre-warm, inspect the queue.

Guarded by a shared token in the `X-Admin-Token` header. While `ADMIN_TOKEN`
is unset they answer 503 and say so, rather than being open by default.
Anything finer-grained (accounts, roles) is out of scope.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.api.deps import SessionDep
from app.core.config import settings
from app.core.errors import ConfigurationError
from app.models.jobs import Job, JobKind, JobSource, JobStatus, PrewarmRun
from app.schemas.admin import (
    DeadJob,
    JobCount,
    PrewarmRunOut,
    PrewarmTriggered,
    QueueSummary,
)
from app.services import prewarm, queue, schedule

DEAD_JOBS_SHOWN = 20


def require_admin(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    if not settings.admin_token:
        raise ConfigurationError("ADMIN_TOKEN is not set; the admin endpoints are disabled.")
    if x_admin_token is None or not secrets.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=401, detail="Missing or wrong X-Admin-Token.")


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post(
    "/prewarm",
    response_model=PrewarmTriggered,
    status_code=202,
    summary="Start a pre-warm run now, outside the nightly schedule",
)
async def trigger_prewarm(
    session: SessionDep,
    trading_date: date | None = Query(
        None,
        description="Which date the run is recorded under. Defaults to today in "
        "the schedule's time zone. One run per date.",
    ),
) -> PrewarmTriggered:
    now = datetime.now(timezone.utc)
    trading_date = trading_date or schedule.trading_date(now)
    existing = await prewarm.run_for(session, trading_date)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A pre-warm run for {trading_date} already exists (run {existing.id}, "
            f"{existing.status.value}). Pass another trading_date to run again.",
        )
    job = await queue.enqueue(
        session,
        JobKind.SCHEDULE_NIGHTLY,
        {"trading_date": trading_date.isoformat(), "manual": True},
        priority=queue.PRIORITY_NIGHTLY_FANOUT,
        dedupe_key=queue.nightly_key(trading_date),
        source=JobSource.SCHEDULED,
    )
    await session.commit()
    return PrewarmTriggered(job_id=job.id, trading_date=trading_date)


@router.get("/queue", response_model=QueueSummary, summary="Queue health and the last run")
async def queue_summary(session: SessionDep) -> QueueSummary:
    now = datetime.now(timezone.utc)
    counts = [
        JobCount(kind=kind, status=status, count=count)
        for kind, status, count in (
            await session.execute(
                sa.select(Job.kind, Job.status, sa.func.count())
                .group_by(Job.kind, Job.status)
                .order_by(Job.kind, Job.status)
            )
        ).all()
    ]
    oldest_due = await session.scalar(
        sa.select(sa.func.min(Job.run_after)).where(
            Job.status == JobStatus.QUEUED, Job.run_after <= now
        )
    )
    dead = (
        await session.scalars(
            sa.select(Job)
            .where(Job.status == JobStatus.DEAD)
            .order_by(Job.finished_at.desc(), Job.id.desc())
            .limit(DEAD_JOBS_SHOWN)
        )
    ).all()
    run = await session.scalar(
        sa.select(PrewarmRun).order_by(PrewarmRun.started_at.desc(), PrewarmRun.id.desc()).limit(1)
    )
    return QueueSummary(
        counts=counts,
        oldest_queued_age_seconds=(
            round((now - _as_utc(oldest_due)).total_seconds(), 1) if oldest_due else None
        ),
        dead=[DeadJob.model_validate(job) for job in dead],
        last_run=_run_out(run) if run else None,
    )


def _run_out(run: PrewarmRun) -> PrewarmRunOut:
    out = PrewarmRunOut.model_validate(run)
    if run.finished_at is not None:
        out.duration_seconds = round(
            (_as_utc(run.finished_at) - _as_utc(run.started_at)).total_seconds(), 1
        )
    return out


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
