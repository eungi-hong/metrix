"""Request ids, liveness and readiness probes, and /metrics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core import metrics
from app.core.config import settings
from app.core.context import CallClass
from app.models.jobs import Job, JobKind, JobSource, JobStatus
from app.models.usage import UsageEvent


async def test_every_response_carries_a_request_id_and_a_safe_one_is_echoed(client):
    fresh = await client.get("/health/live")
    assert len(fresh.headers["X-Request-ID"]) == 32

    echoed = await client.get("/health/live", headers={"X-Request-ID": "trace-abc.123"})
    assert echoed.headers["X-Request-ID"] == "trace-abc.123"

    replaced = await client.get("/health/live", headers={"X-Request-ID": "bad id\nwith newline"})
    assert replaced.headers["X-Request-ID"] != "bad id\nwith newline"
    assert len(replaced.headers["X-Request-ID"]) == 32


async def test_errors_carry_the_request_id_too(client):
    response = await client.get("/no-such-route", headers={"X-Request-ID": "r-404"})
    assert response.status_code == 404 and response.headers["X-Request-ID"] == "r-404"


async def test_liveness_and_readiness(client):
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    ready = await client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["database"] == "ok"


async def test_readiness_is_503_without_the_database(client, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    async def broken(self, *args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(AsyncSession, "execute", broken)
    response = await client.get("/health/ready")
    assert response.status_code == 503 and response.json()["database"] == "unreachable"


async def test_metrics_are_off_without_a_token(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", None)
    assert (await client.get("/metrics")).status_code == 404


async def test_metrics_need_the_token(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "m-token")
    assert (await client.get("/metrics")).status_code == 401
    assert (await client.get("/metrics", headers={"X-Metrics-Token": "wrong"})).status_code == 401


async def test_metrics_report_routes_queue_dead_jobs_and_spend(client, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "m-token")
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        for priority, status, run_after in (
            (0, JobStatus.QUEUED, now - timedelta(seconds=90)),
            (0, JobStatus.RUNNING, now),
            (2, JobStatus.QUEUED, now + timedelta(hours=1)),
            (2, JobStatus.DEAD, now),
        ):
            session.add(
                Job(kind=JobKind.REFRESH_PRICES, payload={}, priority=priority, status=status,
                    dedupe_key=f"k{priority}{status}", run_after=run_after, max_attempts=3,
                    source=JobSource.SCHEDULED)
            )
        session.add(
            UsageEvent(created_at=now, provider="openai", operation="relevance", model="m",
                       input_tokens=1200, output_tokens=300, cost_usd=Decimal("0.0123"),
                       cost_estimated=False, call_class=CallClass.BACKGROUND)
        )
        await session.commit()
    await client.get("/jobs/999999")  # one request on a templated route

    body = (await client.get("/metrics", headers={"X-Metrics-Token": "m-token"})).text

    assert 'metrix_http_request_duration_seconds_count{method="GET",route="/jobs/{job_id}"' in body
    assert 'metrix_queue_depth{priority="0",status="queued"} 1.0' in body
    assert 'metrix_queue_depth{priority="0",status="running"} 1.0' in body
    assert 'metrix_queue_depth{priority="2",status="queued"} 1.0' in body
    lag = next(line for line in body.splitlines() if line.startswith('metrix_queue_lag_seconds{priority="0"}'))
    assert 85 <= float(lag.split()[-1]) < 600
    assert 'metrix_queue_lag_seconds{priority="2"}' not in body  # not due yet
    assert "metrix_dead_jobs 1.0" in body
    assert 'metrix_llm_tokens_today{kind="input",operation="relevance"} 1200.0' in body
    assert 'metrix_spend_usd_today{operation="relevance",provider="openai"} 0.0123' in body
    assert 'metrix_response_cache_requests_total{cache="ticker",outcome="hit"}' in body
    assert "metrix_sse_open_connections" in body


def test_an_unmatched_request_has_a_bounded_route_label():
    from app.api.middleware import RequestContextMiddleware

    middleware = RequestContextMiddleware(app=None)
    assert middleware._route_of({}) == "unmatched"


@pytest.fixture(autouse=True)
def _reset_shared_gauges():
    yield
    metrics.QUEUE_DEPTH.clear()
    metrics.QUEUE_LAG.clear()
