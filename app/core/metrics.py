"""Prometheus metrics.

Two kinds, kept apart on purpose:

* **Per-process instruments** updated as things happen in this process:
  request latency by route template, open server-sent-event streams, and
  response-cache hits and misses. Each API process exposes its own; the
  scraper sums them.
* **Shared state read from the database at scrape time**: queue depth and lag
  by priority, dead jobs, and today's LLM tokens and dollars by operation.
  These are the same whichever process is scraped, and they include the
  worker's work, which no API process sees happen. They are gauges, not
  counters, because they are read rather than counted.

A private registry (not the global default) keeps tests independent and
leaves out the default process collectors' noise.
"""

from __future__ import annotations

from datetime import datetime, time, timezone

import sqlalchemy as sa
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.jobs import Job, JobStatus
from app.models.usage import UsageEvent

REGISTRY = CollectorRegistry(auto_describe=True)
CONTENT_TYPE = CONTENT_TYPE_LATEST

HTTP_LATENCY = Histogram(
    "metrix_http_request_duration_seconds",
    "Request latency by route template.",
    ("method", "route", "status"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=REGISTRY,
)
SSE_OPEN = Gauge("metrix_sse_open_connections", "Open event streams in this process.", registry=REGISTRY)
CACHE_REQUESTS = Counter(
    "metrix_response_cache_requests_total",
    "Response cache lookups by outcome: hit, miss, not_modified (a 304), bypass.",
    ("cache", "outcome"),
    registry=REGISTRY,
)
QUEUE_DEPTH = Gauge("metrix_queue_depth", "Queued and running jobs by priority.", ("priority", "status"), registry=REGISTRY)
QUEUE_LAG = Gauge(
    "metrix_queue_lag_seconds", "Age of the oldest due queued job, by priority.", ("priority",), registry=REGISTRY
)
DEAD_JOBS = Gauge("metrix_dead_jobs", "Jobs the queue gave up on.", registry=REGISTRY)
LLM_TOKENS = Gauge(
    "metrix_llm_tokens_today", "Tokens recorded in the spend ledger today (UTC), by operation and kind.",
    ("operation", "kind"), registry=REGISTRY,
)
LLM_DOLLARS = Gauge(
    "metrix_spend_usd_today", "Dollars recorded in the spend ledger today (UTC), by provider and operation.",
    ("provider", "operation"), registry=REGISTRY,
)


def cache_ratio_labels() -> None:
    """Touch every cache label so a scrape before the first request shows zeros."""
    for outcome in ("hit", "miss", "not_modified", "bypass"):
        CACHE_REQUESTS.labels("ticker", outcome)


cache_ratio_labels()


async def refresh_shared(session: AsyncSession, now: datetime | None = None) -> None:
    """Read the shared gauges from the database."""
    now = now or datetime.now(timezone.utc)
    QUEUE_DEPTH.clear()
    for priority, status, count in (
        await session.execute(
            sa.select(Job.priority, Job.status, sa.func.count())
            .where(Job.status.in_((JobStatus.QUEUED, JobStatus.RUNNING)))
            .group_by(Job.priority, Job.status)
        )
    ).all():
        QUEUE_DEPTH.labels(str(priority), str(getattr(status, "value", status))).set(count)

    QUEUE_LAG.clear()
    for priority, oldest in (
        await session.execute(
            sa.select(Job.priority, sa.func.min(Job.run_after))
            .where(Job.status == JobStatus.QUEUED, Job.run_after <= now)
            .group_by(Job.priority)
        )
    ).all():
        if oldest is not None:
            oldest = oldest if oldest.tzinfo else oldest.replace(tzinfo=timezone.utc)
            QUEUE_LAG.labels(str(priority)).set(max(0.0, (now - oldest).total_seconds()))

    DEAD_JOBS.set(
        await session.scalar(sa.select(sa.func.count()).select_from(Job).where(Job.status == JobStatus.DEAD)) or 0
    )

    midnight = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    LLM_TOKENS.clear()
    LLM_DOLLARS.clear()
    for provider, operation, input_tokens, output_tokens, dollars in (
        await session.execute(
            sa.select(
                UsageEvent.provider,
                UsageEvent.operation,
                sa.func.coalesce(sa.func.sum(UsageEvent.input_tokens), 0),
                sa.func.coalesce(sa.func.sum(UsageEvent.output_tokens), 0),
                sa.func.coalesce(sa.func.sum(UsageEvent.cost_usd), 0),
            )
            .where(UsageEvent.created_at >= midnight)
            .group_by(UsageEvent.provider, UsageEvent.operation)
        )
    ).all():
        if input_tokens or output_tokens:
            LLM_TOKENS.labels(operation, "input").set(float(input_tokens))
            LLM_TOKENS.labels(operation, "output").set(float(output_tokens))
        LLM_DOLLARS.labels(provider, operation).set(float(dollars))


def render() -> bytes:
    return generate_latest(REGISTRY)
