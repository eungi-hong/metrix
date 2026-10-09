"""Operator endpoints: users and keys, pre-warm runs, the queue, the day's
spend, and what a request looks like after the host's proxies.

Guarded by a shared token in the `X-Admin-Token` header. While `ADMIN_TOKEN`
is unset they answer 503 and say so, rather than being open by default.
Anything finer-grained (accounts, roles) is out of scope.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import SessionDep, require_admin
from app.core.config import settings
from app.core.context import CallClass
from app.models.identity import ApiKey, Plan, User
from app.models.jobs import Job, JobKind, JobSource, JobStatus, PrewarmRun
from app.models.usage import UsageEvent
from app.schemas.admin import (
    DeadJob,
    JobCount,
    KeyCreate,
    KeyCreated,
    KeyOut,
    PrewarmRunOut,
    PrewarmTriggered,
    QueueSummary,
    RequestInfo,
    SpendByOperation,
    SpendByUser,
    UsageSummary,
    UserCreate,
    UserOut,
    UserPatch,
)
from app.services import auth, prewarm, queue, schedule, spend

DEAD_JOBS_SHOWN = 20
TOP_USERS_SHOWN = 10


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
    held = await session.scalar(
        sa.select(sa.func.count())
        .select_from(Job)
        .where(Job.status == JobStatus.QUEUED, Job.hold_reason == queue.HOLD_SPEND_CAP)
    )
    return QueueSummary(
        held_by_spend_cap=held or 0,
        counts=counts,
        oldest_queued_age_seconds=(
            round((now - _as_utc(oldest_due)).total_seconds(), 1) if oldest_due else None
        ),
        dead=[DeadJob.model_validate(job) for job in dead],
        last_run=_run_out(run) if run else None,
    )


@router.get("/usage", response_model=UsageSummary, summary="Where the money went on one day")
async def usage_summary(
    session: SessionDep,
    day: date | None = Query(
        None, alias="date", description="UTC date. Defaults to today."
    ),
) -> UsageSummary:
    day = day or spend.utc_day(datetime.now(timezone.utc))
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    on_day = [UsageEvent.created_at >= start, UsageEvent.created_at < start + timedelta(days=1)]

    by_operation = [
        SpendByOperation(
            provider=provider,
            operation=operation,
            calls=calls,
            cost_usd=spend.usd(cost or 0),
            estimated_calls=estimated or 0,
        )
        for provider, operation, calls, cost, estimated in (
            await session.execute(
                sa.select(
                    UsageEvent.provider,
                    UsageEvent.operation,
                    sa.func.count(),
                    sa.func.sum(UsageEvent.cost_usd),
                    sa.func.sum(sa.case((UsageEvent.cost_estimated, 1), else_=0)),
                )
                .where(*on_day)
                .group_by(UsageEvent.provider, UsageEvent.operation)
                .order_by(sa.func.sum(UsageEvent.cost_usd).desc())
            )
        ).all()
    ]
    by_class = dict(
        (
            await session.execute(
                sa.select(UsageEvent.call_class, sa.func.sum(UsageEvent.cost_usd))
                .where(*on_day)
                .group_by(UsageEvent.call_class)
            )
        ).all()
    )
    top_users = [
        SpendByUser(user_id=user_id, calls=calls, cost_usd=spend.usd(cost or 0))
        for user_id, calls, cost in (
            await session.execute(
                sa.select(UsageEvent.user_id, sa.func.count(), sa.func.sum(UsageEvent.cost_usd))
                .where(*on_day, UsageEvent.user_id.is_not(None))
                .group_by(UsageEvent.user_id)
                .order_by(sa.func.sum(UsageEvent.cost_usd).desc())
                .limit(TOP_USERS_SHOWN)
            )
        ).all()
    ]
    current = await spend.status(session, day)
    background = spend.usd(by_class.get(CallClass.BACKGROUND) or 0)
    interactive = spend.usd(by_class.get(CallClass.INTERACTIVE) or 0)
    return UsageSummary(
        date=day,
        total_usd=background + interactive,
        background_usd=background,
        interactive_usd=interactive,
        calls=sum(row.calls for row in by_operation),
        by_operation=by_operation,
        top_users=top_users,
        cap_usd=current.cap_usd,
        background_cap_usd=current.background_cap_usd,
        reserved_usd=current.reserved_usd,
        headroom_usd=current.headroom_usd,
        background_headroom_usd=current.background_headroom_usd,
        interactive_capped=current.interactive_capped,
        background_capped=current.background_capped,
    )


@router.post("/users", response_model=UserOut, status_code=201, summary="Create a user")
async def create_user(body: UserCreate, session: SessionDep) -> UserOut:
    if body.email and await session.scalar(sa.select(User.id).where(User.email == body.email)):
        raise HTTPException(status_code=409, detail=f"A user with email {body.email} exists.")
    user = User(name=body.name, email=body.email, plan=Plan(body.plan))
    session.add(user)
    await session.commit()
    return UserOut.model_validate(user)


@router.patch("/users/{user_id}", response_model=UserOut, summary="Change a user's plan, or disable them")
async def update_user(user_id: int, body: UserPatch, session: SessionDep) -> UserOut:
    user = await _user(session, user_id)
    if body.plan is not None:
        user.plan = Plan(body.plan)
    if body.disabled is True and user.disabled_at is None:
        user.disabled_at = datetime.now(timezone.utc)
    elif body.disabled is False:
        user.disabled_at = None
    await session.commit()
    return UserOut.model_validate(user)


@router.post(
    "/users/{user_id}/keys",
    response_model=KeyCreated,
    status_code=201,
    summary="Issue an API key. The response is the only time the key is shown",
)
async def create_key(user_id: int, body: KeyCreate, session: SessionDep) -> KeyCreated:
    user = await _user(session, user_id)
    row, key = await auth.issue_key(session, user, label=body.label)
    await session.commit()
    return KeyCreated(**KeyOut.model_validate(row).model_dump(), key=key)


@router.delete("/keys/{key_id}", response_model=KeyOut, summary="Revoke an API key")
async def revoke_key(key_id: int, session: SessionDep) -> KeyOut:
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No API key with id {key_id}.")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        await session.commit()
    return KeyOut.model_validate(row)


@router.get(
    "/request-info",
    response_model=RequestInfo,
    summary="What this request looks like on arrival, to set TRUSTED_PROXY_COUNT",
)
async def request_info(request: Request) -> RequestInfo:
    """How many proxies sit in front of the API cannot be read from the
    configuration; it has to be measured. Call this through the public URL,
    once as is and once with a forged X-Forwarded-For: the right
    TRUSTED_PROXY_COUNT is the one that gives your real address both times.
    Only forwarding headers are echoed back."""
    peer = request.client.host if request.client else None
    forwarded_for = request.headers.get("x-forwarded-for")
    hops = [hop.strip() for hop in (forwarded_for or "").split(",") if hop.strip()]
    return RequestInfo(
        peer=peer,
        x_forwarded_for=forwarded_for,
        hops=hops,
        x_real_ip=request.headers.get("x-real-ip"),
        forwarded=request.headers.get("forwarded"),
        trusted_proxy_count=settings.trusted_proxy_count,
        client_ip=auth.client_ip(peer, forwarded_for),
        client_ip_by_trusted_count={
            0: peer or "unknown",
            **{n: hops[-n] for n in range(1, len(hops) + 1)},
        },
    )


async def _user(session: SessionDep, user_id: int) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"No user with id {user_id}.")
    return user


def _run_out(run: PrewarmRun) -> PrewarmRunOut:
    out = PrewarmRunOut.model_validate(run)
    if run.finished_at is not None:
        out.duration_seconds = round(
            (_as_utc(run.finished_at) - _as_utc(run.started_at)).total_seconds(), 1
        )
    return out


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
