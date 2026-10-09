"""Per-user quotas: what each plan may do, charged against `app.services.limits`.

Every count is per principal: a user, or an anonymous caller's IP address
(`Principal.key`). Limits per plan come from PLAN_LIMITS_JSON.

    requests_per_minute     every authenticated API call, cheap reads included
    chat_per_minute/day     chat turns
    cold_ingests_per_day    requests that start new ingestion or news work
    refresh_per_day         refresh=true requests that cause work

Charge, then refund on our failure
----------------------------------
A quota is charged before the work, so a burst of concurrent requests cannot
all slip past it, and refunded if the work then fails for a reason that is
ours: an upstream outage, or the spend cap. A user should not lose quota to our
outage. A request that fails for the caller's own reason keeps its charge, so
quota cannot be used to probe for free.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from app.core.config import PlanLimits, settings
from app.core.errors import RateLimited
from app.core.logging import get_logger
from app.services.auth import Principal
from app.services.limits import LimitResult, get_limiter

logger = get_logger(__name__)

SECONDS_PER_MINUTE = 60.0


class Quota(StrEnum):
    REQUESTS_PER_MINUTE = "requests_per_minute"
    CHAT_PER_MINUTE = "chat_per_minute"
    CHAT_PER_DAY = "chat_per_day"
    COLD_INGESTS_PER_DAY = "cold_ingests_per_day"
    REFRESH_PER_DAY = "refresh_per_day"


PER_MINUTE = {Quota.REQUESTS_PER_MINUTE, Quota.CHAT_PER_MINUTE}


def limits_for(principal: Principal) -> PlanLimits:
    return settings.plan_limits_json[principal.plan.value]


def _key(quota: Quota, principal: Principal) -> str:
    return f"quota:{quota.value}:{principal.key}"


def _today() -> datetime:
    return datetime.now(timezone.utc)


async def charge(principal: Principal, quota: Quota, *, cost: int = 1) -> LimitResult:
    """Take `cost` from the quota, or raise `RateLimited` having taken nothing."""
    limit = getattr(limits_for(principal), quota.value)
    limiter = get_limiter()
    if quota in PER_MINUTE:
        result = await limiter.gcra(
            _key(quota, principal), limit=limit, period=SECONDS_PER_MINUTE, cost=cost
        )
    else:
        result = await limiter.daily(
            _key(quota, principal), limit=limit, day=_today().date(), cost=cost
        )
    if not result.allowed:
        logger.info(
            "quota_exceeded",
            principal=principal.key,
            plan=principal.plan.value,
            quota=quota.value,
            limit=limit,
            retry_after_s=round(result.retry_after, 1),
        )
        raise RateLimited(quota.value, result)
    return result


async def refund(principal: Principal, quota: Quota, *, cost: int = 1) -> None:
    """Give back a charge whose work failed for a reason of ours. Best-effort."""
    limiter = get_limiter()
    try:
        if quota in PER_MINUTE:
            limit = getattr(limits_for(principal), quota.value)
            await limiter.gcra_refund(
                _key(quota, principal), limit=limit, period=SECONDS_PER_MINUTE, cost=cost
            )
        else:
            await limiter.daily_refund(_key(quota, principal), day=_today().date(), cost=cost)
    except Exception as exc:
        logger.warning("quota_refund_failed", quota=quota.value, error=str(exc))
