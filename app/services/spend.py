"""The spend ledger and the daily spend cap.

Every billable external call (an Exa search, an LLM call) goes through
`metered`, from the provider seam itself, so no caller can forget it:

    async with spend.metered("exa", "search", estimate) as meter:
        payload = await post(...)
        await meter.settle(cost_usd=..., estimated=...)

Reserve, then settle
--------------------
Before the call, `reserve` sets the call's estimated cost aside on today's
`spend_daily` row, in one conditional UPDATE that succeeds only if the
reservation still fits under the cap (and, for background calls, under
background's share of it). If it does not fit, the call is refused with
`SpendCapReached` and never made. After the call, `settle` records the actual
cost in `usage_events` and moves the amount from reserved to spent; a call
that fails releases its reservation instead.

Checking the cap with a plain read, then calling, would let N concurrent
calls all read the same headroom and all proceed: an overshoot of up to N
times the largest call. Reserving closes that race, because the database
serializes the conditional UPDATEs on the row. What remains is estimate
error: the cap can be overshot only by the amount actual costs exceed their
estimates, summed over the calls in flight when it is reached. Estimates are
built to be high: an LLM call is estimated at its full `max_tokens` of output
and at least one input token per CONSERVATIVE_CHARS_PER_TOKEN characters of
prompt, and an Exa search at EXA_COST_ESTIMATE_USD. So the realistic
overshoot is zero, and it is bounded by (calls in flight) x (the largest
amount by which a call can beat its estimate), which for the LLM is nothing
and for Exa is the gap between a search's real cost and EXA_COST_ESTIMATE_USD.

A reservation whose process dies before it settles stays on the row until
the day ends. That overcounts, which is the safe direction for a cap, and
`GET /admin/usage` shows reservations separately so it is visible.

The shares
----------
Background calls (the nightly run, follow-ups) may use at most
BACKGROUND_SPEND_SHARE of the cap, so a heavy night can never leave users with
nothing; interactive calls may use the whole cap. Which class a call belongs
to comes from `app.core.context`, set by the API per request and by the worker
per job.

Failure policy
--------------
Settling is best-effort, like `demand.record_demand`: a ledger write that
fails is logged and never fails the call it records, whose result is already
paid for. Reserving is not: if the database cannot be reached, the reserve
raises and the call is not made. Money fails closed.

The ledger uses its own sessions, not the caller's, so a reservation commits
at once instead of waiting on whatever transaction the caller has open, and a
caller's rollback cannot erase a cost that was really incurred.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import context
from app.core.config import LLMPrice, settings
from app.core.context import Attribution, CallClass
from app.core.errors import SpendCapReached
from app.core.logging import get_logger
from app.models.usage import SpendDaily, UsageEvent

logger = get_logger(__name__)

TOKENS_PER_MTOK = 1_000_000
# English prose runs at about four characters per token. Estimating input at
# one token per two characters overstates it on purpose: the estimate is what
# the cap is enforced against, so it has to be high, not accurate.
CONSERVATIVE_CHARS_PER_TOKEN = 2
MICRO = Decimal("0.000001")


# --------------------------------------------------------------- sessions

_session_factory: async_sessionmaker[AsyncSession] | None = None


def configure(session_factory: async_sessionmaker[AsyncSession] | None) -> None:
    """Point the ledger at a session factory. None restores the default,
    the application's `SessionLocal`."""
    global _session_factory
    _session_factory = session_factory


def _sessions() -> async_sessionmaker[AsyncSession]:
    if _session_factory is not None:
        return _session_factory
    from app.db.session import SessionLocal  # deferred: tests never build the real engine

    return SessionLocal


# ------------------------------------------------------------------- time


def _now() -> datetime:
    return datetime.now(timezone.utc)


def utc_day(now: datetime) -> date:
    return now.astimezone(timezone.utc).date()


def next_reset(now: datetime) -> datetime:
    """The next 00:00 UTC, when the day's cap starts again from zero."""
    return datetime.combine(utc_day(now) + timedelta(days=1), time.min, tzinfo=timezone.utc)


# ----------------------------------------------------------------- prices


def usd(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(MICRO, rounding=ROUND_HALF_UP)


def llm_price(model: str) -> tuple[LLMPrice, bool]:
    """The model's prices, and whether they are the fallback (an estimate)."""
    price = settings.llm_prices_json.get(model)
    if price is not None:
        return price, False
    fallback_in = settings.llm_fallback_input_per_mtok
    return (
        LLMPrice(
            input_per_mtok=fallback_in,
            output_per_mtok=settings.llm_fallback_output_per_mtok,
            cache_read_per_mtok=fallback_in,
            cache_write_per_mtok=fallback_in,
        ),
        True,
    )


def llm_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> tuple[Decimal, bool]:
    """What a finished call cost, and whether that figure is an estimate."""
    price, estimated = llm_price(model)
    dollars = (
        input_tokens * price.input_per_mtok
        + output_tokens * price.output_per_mtok
        + cache_read_tokens * price.cache_read_per_mtok
        + cache_write_tokens * price.cache_write_per_mtok
    ) / TOKENS_PER_MTOK
    return usd(dollars), estimated


def llm_estimate(model: str, *, prompt_chars: int, max_tokens: int) -> Decimal:
    """A high estimate of a call's cost, made before it, to reserve against the cap."""
    price, _ = llm_price(model)
    input_tokens = math.ceil(prompt_chars / CONSERVATIVE_CHARS_PER_TOKEN)
    # Priced at the dearer of plain and cache-write input, whichever the call turns out to use.
    input_rate = max(price.input_per_mtok, price.cache_write_per_mtok)
    dollars = (input_tokens * input_rate + max_tokens * price.output_per_mtok) / TOKENS_PER_MTOK
    return usd(dollars)


def exa_cost(payload: dict[str, Any]) -> tuple[Decimal, bool]:
    """Exa's own figure for a search, or the configured estimate if it gave none."""
    total = (payload.get("costDollars") or {}).get("total")
    if isinstance(total, (int, float)) and total >= 0:
        return usd(total), False
    return usd(settings.exa_cost_estimate_usd), True


def exa_estimate() -> Decimal:
    return usd(settings.exa_cost_estimate_usd)


# ------------------------------------------------------------------- caps


def daily_cap() -> Decimal | None:
    cap = settings.daily_spend_cap_usd
    return None if cap is None else usd(cap)


def background_cap() -> Decimal | None:
    cap = settings.daily_spend_cap_usd
    return None if cap is None else usd(cap * settings.background_spend_share)


# ----------------------------------------------------------- reservations


@dataclass(slots=True)
class Reservation:
    """An estimate set aside on `day`'s row, until `settle` or `release`."""

    provider: str
    operation: str
    estimate: Decimal
    day: date
    attribution: Attribution
    done: bool = field(default=False)

    @property
    def background(self) -> bool:
        return self.attribution.call_class == CallClass.BACKGROUND

    async def settle(
        self,
        *,
        cost_usd: Decimal,
        estimated: bool,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> None:
        """Record the call and its cost; replace the reservation with it.

        Best-effort: a failure is logged, never raised. The cost is added to
        today's row, the date of the ledger row, even if the reservation was
        made before midnight; the reservation is released on its own day.
        """
        if self.done:
            return
        self.done = True
        now = _now()
        today = utc_day(now)
        try:
            async with _sessions()() as session:
                session.add(
                    UsageEvent(
                        created_at=now,
                        provider=self.provider,
                        operation=self.operation,
                        model=model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read_tokens,
                        cache_write_tokens=cache_write_tokens,
                        cost_usd=cost_usd,
                        cost_estimated=estimated,
                        user_id=self.attribution.user_id,
                        job_id=self.attribution.job_id,
                        call_class=self.attribution.call_class,
                    )
                )
                await _ensure_day(session, today, now)
                await _unreserve(session, self, now)
                spent: dict[str, Any] = {
                    "spent_usd": SpendDaily.spent_usd + cost_usd,
                    "updated_at": now,
                }
                if self.background:
                    spent["background_spent_usd"] = SpendDaily.background_spent_usd + cost_usd
                await session.execute(
                    sa.update(SpendDaily).where(SpendDaily.day == today).values(**spent)
                )
                crossed = await _mark_alert(session, today, now)
                await session.commit()
        except Exception as exc:
            logger.warning(
                "spend_record_failed",
                provider=self.provider,
                operation=self.operation,
                cost_usd=str(cost_usd),
                error=str(exc),
            )
            return

        logger.info(
            "spend_recorded",
            provider=self.provider,
            operation=self.operation,
            model=model,
            cost_usd=str(cost_usd),
            cost_estimated=estimated,
            call_class=self.attribution.call_class.value,
            user_id=self.attribution.user_id,
            job_id=self.attribution.job_id,
        )
        if crossed:
            logger.warning(
                "spend_threshold_crossed",
                day=today.isoformat(),
                fraction=settings.spend_alert_fraction,
                cap_usd=str(daily_cap()),
            )

    async def release(self) -> None:
        """Give the reservation back: the call failed and cost nothing. Best-effort."""
        if self.done:
            return
        self.done = True
        try:
            async with _sessions()() as session:
                await _unreserve(session, self, _now())
                await session.commit()
        except Exception as exc:
            logger.warning(
                "spend_release_failed",
                provider=self.provider,
                operation=self.operation,
                error=str(exc),
            )


async def reserve(provider: str, operation: str, estimate: Decimal) -> Reservation:
    """Set `estimate` aside under today's cap, or raise `SpendCapReached`.

    Raises the database's own error if it cannot be reached: no reservation,
    no call.
    """
    attribution = context.current()
    background = attribution.call_class == CallClass.BACKGROUND
    now = _now()
    today = utc_day(now)
    cap, share = daily_cap(), background_cap()

    async with _sessions()() as session:
        await _ensure_day(session, today, now)
        conditions: list[sa.ColumnElement[bool]] = [SpendDaily.day == today]
        if cap is not None:
            conditions.append(SpendDaily.spent_usd + SpendDaily.reserved_usd + estimate <= cap)
        if share is not None and background:
            conditions.append(
                SpendDaily.background_spent_usd + SpendDaily.background_reserved_usd + estimate
                <= share
            )
        values: dict[str, Any] = {
            "reserved_usd": SpendDaily.reserved_usd + estimate,
            "updated_at": now,
        }
        if background:
            values["background_reserved_usd"] = SpendDaily.background_reserved_usd + estimate
        result = await session.execute(
            sa.update(SpendDaily)
            .where(*conditions)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount:
            await session.commit()
            return Reservation(
                provider=provider,
                operation=operation,
                estimate=estimate,
                day=today,
                attribution=attribution,
            )

        first_today = await _mark_cap_logged(session, today, attribution.call_class, now)
        await session.commit()

    retry_at = next_reset(now)
    limit = share if background else cap
    if first_today:
        logger.warning(
            "spend_cap_reached",
            call_class=attribution.call_class.value,
            day=today.isoformat(),
            limit_usd=str(limit),
            provider=provider,
            operation=operation,
        )
    scope = "background share of the daily spend cap" if background else "daily spend cap"
    raise SpendCapReached(
        attribution.call_class.value,
        retry_at,
        f"The {scope} (${limit}) is reached; it resets at {retry_at.isoformat()}.",
    )


@asynccontextmanager
async def metered(provider: str, operation: str, estimate: Decimal) -> AsyncIterator[Reservation]:
    """Reserve, run the body, and release the reservation unless it was settled.

    A body that raises (the call failed) releases it. So does one that
    returns without settling, which is a bug in the seam and is logged.
    """
    reservation = await reserve(provider, operation, estimate)
    try:
        yield reservation
    except BaseException:
        await reservation.release()
        raise
    if not reservation.done:
        logger.warning("spend_reservation_unsettled", provider=provider, operation=operation)
        await reservation.release()


# ----------------------------------------------------------------- status


@dataclass(frozen=True, slots=True)
class SpendStatus:
    day: date
    cap_usd: Decimal | None
    background_cap_usd: Decimal | None
    spent_usd: Decimal
    background_spent_usd: Decimal
    reserved_usd: Decimal
    background_reserved_usd: Decimal
    interactive_capped: bool
    background_capped: bool

    @property
    def headroom_usd(self) -> Decimal | None:
        if self.cap_usd is None:
            return None
        return max(self.cap_usd - self.spent_usd - self.reserved_usd, Decimal(0))

    @property
    def background_headroom_usd(self) -> Decimal | None:
        if self.background_cap_usd is None or self.headroom_usd is None:
            return None
        own = self.background_cap_usd - self.background_spent_usd - self.background_reserved_usd
        return max(min(own, self.headroom_usd), Decimal(0))


async def status(session: AsyncSession, day: date | None = None) -> SpendStatus:
    """The day's figures, from the caller's session."""
    day = day or utc_day(_now())
    row = await session.get(SpendDaily, day, populate_existing=True)
    zero = usd(0)
    return SpendStatus(
        day=day,
        cap_usd=daily_cap(),
        background_cap_usd=background_cap(),
        spent_usd=usd(row.spent_usd) if row else zero,
        background_spent_usd=usd(row.background_spent_usd) if row else zero,
        reserved_usd=usd(row.reserved_usd) if row else zero,
        background_reserved_usd=usd(row.background_reserved_usd) if row else zero,
        interactive_capped=bool(row and row.interactive_cap_logged_at),
        background_capped=bool(row and row.background_cap_logged_at),
    )


async def refused_today(session: AsyncSession, call_class: CallClass) -> bool:
    """Whether a call of this class has already been refused by the cap today.

    Cheap enough for a request path, and exactly the signal a user needs:
    work queued now will wait for tomorrow's budget.
    """
    if daily_cap() is None:
        return False
    current = await status(session)
    return (
        current.interactive_capped
        if call_class == CallClass.INTERACTIVE
        else current.background_capped
    )


def warn_on_startup() -> None:
    """Say loudly, once per process, what the ledger cannot price or bound."""
    if settings.llm_model not in settings.llm_prices_json:
        logger.error(
            "llm_model_unpriced",
            model=settings.llm_model,
            detail="LLM_MODEL has no entry in LLM_PRICES_JSON. Its calls are recorded "
            "at the fallback rates and flagged cost_estimated=true.",
            fallback_input_per_mtok=settings.llm_fallback_input_per_mtok,
            fallback_output_per_mtok=settings.llm_fallback_output_per_mtok,
        )
    if settings.daily_spend_cap_usd is None:
        logger.warning(
            "spend_cap_unset",
            detail="DAILY_SPEND_CAP_USD is unset: spend is recorded but not limited.",
            env=settings.app_env,
        )


# ---------------------------------------------------------------- helpers


async def _ensure_day(session: AsyncSession, day: date, now: datetime) -> None:
    """Create the day's row if it is missing; concurrent creators are harmless."""
    insert = pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert
    await session.execute(
        insert(SpendDaily)
        .values(
            day=day,
            spent_usd=Decimal(0),
            background_spent_usd=Decimal(0),
            reserved_usd=Decimal(0),
            background_reserved_usd=Decimal(0),
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[SpendDaily.day])
    )


async def _unreserve(session: AsyncSession, reservation: Reservation, now: datetime) -> None:
    values: dict[str, Any] = {
        "reserved_usd": SpendDaily.reserved_usd - reservation.estimate,
        "updated_at": now,
    }
    if reservation.background:
        values["background_reserved_usd"] = (
            SpendDaily.background_reserved_usd - reservation.estimate
        )
    await session.execute(
        sa.update(SpendDaily)
        .where(SpendDaily.day == reservation.day)
        .values(**values)
        .execution_options(synchronize_session=False)
    )


async def _mark_alert(session: AsyncSession, day: date, now: datetime) -> bool:
    """Stamp the day's alert if settled spend has crossed the threshold.
    True only for the call that stamps it, so the alert logs once a day."""
    cap = daily_cap()
    if cap is None:
        return False
    threshold = usd(cap * Decimal(str(settings.spend_alert_fraction)))
    result = await session.execute(
        sa.update(SpendDaily)
        .where(
            SpendDaily.day == day,
            SpendDaily.alert_logged_at.is_(None),
            SpendDaily.spent_usd >= threshold,
        )
        .values(alert_logged_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def _mark_cap_logged(
    session: AsyncSession, day: date, call_class: CallClass, now: datetime
) -> bool:
    column = (
        SpendDaily.interactive_cap_logged_at
        if call_class == CallClass.INTERACTIVE
        else SpendDaily.background_cap_logged_at
    )
    result = await session.execute(
        sa.update(SpendDaily)
        .where(SpendDaily.day == day, column.is_(None))
        .values({column: now})
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)
