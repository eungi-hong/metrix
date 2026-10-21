"""Where a movement's evidence stands, and when it will be there.

Every movement gets an `EvidenceStatus`, so a client can say "researching",
"window still open until ..." or "deferred by today's spend cap" instead of
showing an empty list:

    explained       linked news, or a filing scorer v2 judged relevant
    abstained       scorer v2 judged that nothing found explains it
    no_evidence     searched after the window closed; nothing explained it
    researching     its enrichment is running now
    queued          an enrichment is queued (`expected_by` is when it may start)
    budget_deferred queued, but held by today's spend cap until 00:00 UTC
    window_open     searched once; the news window is still open, and the
                    follow-up runs when it closes (`window_closes_at`)
    retrying        the last attempt failed; it is retried in the nightly run
    failed          it failed NEWS_MAX_ATTEMPTS times; only a refresh retries
    not_started     not researched yet: it is queued when the ticker is next
                    requested, or by the nightly run if the ticker is in its
                    universe (`expected_by` is the next run)

The status is built from stored fields and the movement's active enrichment
job only, never from the clock, except `expected_by` for `not_started`, which
moves once a day; the read path's ETag includes both (`fingerprint`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import NewsStatus
from app.models.jobs import ACTIVE_JOB_STATUSES, Job, JobKind, JobStatus
from app.models.market import Movement
from app.services import queue, schedule


@dataclass(frozen=True, slots=True)
class ActiveJob:
    status: str
    hold_reason: str | None
    run_after: datetime


@dataclass(frozen=True, slots=True)
class EvidenceStatus:
    state: str
    detail: str
    window_closes_at: datetime | None = None
    expected_by: datetime | None = None


async def active_jobs(session: AsyncSession, ticker_id: int) -> dict[int, ActiveJob]:
    """Active enrichment jobs of the ticker's unfinished movements, by movement id."""
    unfinished = (
        await session.scalars(
            sa.select(Movement.id).where(
                Movement.ticker_id == ticker_id, Movement.news_status != NewsStatus.COMPLETE
            )
        )
    ).all()
    if not unfinished:
        return {}
    keys = {queue.enrich_key(mid): mid for mid in unfinished}
    rows = (
        await session.execute(
            sa.select(Job.dedupe_key, Job.status, Job.hold_reason, Job.run_after).where(
                Job.kind == JobKind.ENRICH_MOVEMENT,
                Job.status.in_(ACTIVE_JOB_STATUSES),
                Job.dedupe_key.in_(list(keys)),
            )
        )
    ).all()
    return {
        keys[key]: ActiveJob(str(getattr(status, "value", status)), hold, _utc(run_after))
        for key, status, hold, run_after in rows
    }


def fingerprint(jobs: dict[int, ActiveJob], now: datetime) -> list:
    """What, besides the data version, the statuses depend on."""
    return [
        sorted((mid, j.status, j.hold_reason, j.run_after.isoformat()) for mid, j in jobs.items()),
        schedule.next_run_after(now).isoformat(),
    ]


def status_for(
    movement: Movement,
    *,
    has_evidence: bool,
    job: ActiveJob | None,
    now: datetime,
) -> EvidenceStatus:
    closes_at = _utc(movement.news_window_closes_at) if movement.news_window_closes_at else None
    if has_evidence:
        return EvidenceStatus("explained", "Linked evidence explains this move.", closes_at)
    if movement.evidence_abstained:
        return EvidenceStatus(
            "abstained", movement.abstain_reason or "Nothing found explains this move.", closes_at
        )
    if movement.news_status == NewsStatus.COMPLETE:
        return EvidenceStatus(
            "no_evidence", "Searched after the news window closed; nothing found explains it.", closes_at
        )
    if job is not None:
        if job.status == JobStatus.RUNNING.value:
            return EvidenceStatus("researching", "Being researched now.", closes_at)
        if job.hold_reason == queue.HOLD_SPEND_CAP:
            reset = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=timezone.utc)
            return EvidenceStatus(
                "budget_deferred", "Queued, but held by today's spend cap until 00:00 UTC.", closes_at, reset
            )
        if movement.news_status == NewsStatus.PARTIAL:
            return EvidenceStatus(
                "window_open",
                "Searched once; the news window is still open and it is searched again when it closes.",
                closes_at,
                job.run_after,
            )
        return EvidenceStatus("queued", "Queued for research.", closes_at, job.run_after)
    if movement.news_status == NewsStatus.PARTIAL:
        return EvidenceStatus(
            "window_open",
            "Searched once; the news window is still open and it is searched again after it closes.",
            closes_at,
            schedule.next_run_after(max(now, closes_at or now)),
        )
    if movement.news_status == NewsStatus.FAILED:
        if movement.news_attempts < settings.news_max_attempts:
            return EvidenceStatus(
                "retrying", "The last attempt failed; it is retried in the nightly run.", closes_at,
                schedule.next_run_after(now),
            )
        return EvidenceStatus("failed", "The news search failed, so this move has no explanation yet. It is retried on the next refresh.", closes_at)
    return EvidenceStatus(
        "not_started",
        "Not researched yet: queued when this ticker is next requested, or by the nightly run.",
        closes_at,
        schedule.next_run_after(now),
    )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
