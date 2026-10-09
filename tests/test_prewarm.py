"""Tests for the nightly pre-warm: priorities, the price-refresh chunks, the
budget, the scheduler tick, and one whole night end to end on SQLite."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from httpx import AsyncClient

from app.core.config import settings
from app.core.errors import PriceDataError, TickerNotFoundError
from app.models.enums import IngestStatus, NewsStatus
from app.models.jobs import Job, JobKind, JobSource, JobStatus, PrewarmRun, PrewarmRunStatus
from app.models.market import Movement
from app.services import demand, ingestion, prewarm, queue
from app.services import prices as price_service
from app.services.demand import UniverseEntry
from app.services.news.fixture import FixtureNewsProvider
from app.worker import Worker
from tests.conftest import StubLLM, api_app, api_client, build_price_history, new_api_key
from tests.test_freshness import history_ending_on

TODAY = datetime.now(timezone.utc).date()


@pytest.fixture(autouse=True)
def nightly_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "prewarm_seed_symbols", "")
    monkeypatch.setattr(settings, "prewarm_seed_file", str(tmp_path / "no-seeds.txt"))
    monkeypatch.setattr(settings, "prewarm_max_enrichments_per_run", 300)
    monkeypatch.setattr(settings, "price_batch_size", 100)


@pytest.fixture
def worker(db, stub_llm) -> Worker:
    return Worker(session_factory=db, llm=stub_llm, concurrency=2, interactive_slots=1)


class PriceFeed:
    """Stands in for `fetch_price_histories`, and records how it was asked."""

    def __init__(self, histories: dict[str, object]) -> None:
        self.histories = histories
        self.calls: list[tuple[list[str], int, list[str]]] = []

    async def __call__(self, symbols, days, *, with_profile=()):
        self.calls.append((sorted(symbols), days, sorted(with_profile)))
        return {s: self.histories.get(s, TickerNotFoundError(s)) for s in symbols}


def install_feed(monkeypatch, histories: dict[str, object]) -> PriceFeed:
    feed = PriceFeed(histories)
    monkeypatch.setattr(price_service, "fetch_price_histories", feed)
    return feed


def entry(symbol: str, popularity: float = 1.0, seeded: bool = False) -> UniverseEntry:
    return UniverseEntry(symbol=symbol, popularity=popularity, seeded=seeded)


async def drain(worker: Worker, limit: int = 200) -> list[Job]:
    """Run jobs until none is due. Returns them in the order they ran."""
    ran = []
    for _ in range(limit):
        job = await worker.run_once()
        if job is None:
            return ran
        ran.append(job)
    raise AssertionError("the queue did not drain")


async def all_of(db, model, *where):
    async with db() as session:
        return list((await session.scalars(sa.select(model).where(*where))).all())


async def new_run(db, budget: int = 300) -> PrewarmRun:
    async with db() as session:
        run = await prewarm.start_run(session, TODAY, now=datetime.now(timezone.utc))
        run.enrichment_budget = budget
        await session.commit()
        return run


# ------------------------------------------------------------ priorities


def test_popularity_decides_and_move_size_breaks_ties():
    hot = prewarm.enrichment_priority(600.0, 0.02, seeded=False)
    daily = prewarm.enrichment_priority(10.6, 0.02, seeded=False)
    once = prewarm.enrichment_priority(1.0, 0.12, seeded=False)
    assert hot < daily < once, "more popular runs first, whatever the move"

    big = prewarm.enrichment_priority(10.6, 0.12, seeded=False)
    small = prewarm.enrichment_priority(10.6, 0.02, seeded=False)
    assert big < small, "same popularity: the bigger move first"


@pytest.mark.parametrize("popularity", [0.01, 0.3, 1, 5, 10.6, 100, 1e6])
@pytest.mark.parametrize("move", [0.0, 0.031, 0.05, 0.2])
def test_demanded_enrichment_stays_in_its_band(popularity, move):
    assert 20 <= prewarm.enrichment_priority(popularity, move, seeded=False) <= 59
    # A seed that also has demand is prioritised by its demand.
    assert 20 <= prewarm.enrichment_priority(popularity, move, seeded=True) <= 59


@pytest.mark.parametrize("move", [0.0, 0.031, 0.05, 0.2])
def test_seed_only_enrichment_comes_after_all_demand(move):
    priority = prewarm.enrichment_priority(0.0, move, seeded=True)
    assert 60 <= priority <= 89
    assert priority > prewarm.enrichment_priority(0.001, 0.0, seeded=False)


def test_bands_sit_between_macro_and_followups():
    assert queue.PRIORITY_INTERACTIVE < queue.PRIORITY_NIGHTLY_FANOUT < queue.PRIORITY_MACRO
    assert queue.PRIORITY_MACRO < prewarm.DEMAND_BAND_START
    assert prewarm.enrichment_priority(0.0, 0.0, seeded=True) < queue.PRIORITY_FOLLOWUP


@pytest.mark.parametrize(
    ("days_since_last_bar", "full"),
    [(None, True), (1, False), (30, False), (38, False), (39, True), (400, True)],
)
def test_the_short_window_is_used_only_when_it_overlaps(monkeypatch, days_since_last_bar, full):
    monkeypatch.setattr(settings, "prewarm_price_lookback_days", 45)
    last = None if days_since_last_bar is None else TODAY - timedelta(days=days_since_last_bar)
    assert prewarm.needs_full_history(last, TODAY) is full


# ------------------------------------------------------- needs_enrichment


async def test_the_sql_and_python_enrichment_rules_agree(session, monkeypatch):
    monkeypatch.setattr(settings, "news_max_attempts", 3)
    now = datetime.now(timezone.utc)
    ticker = await ingestion.get_or_create_ticker(session, "RULES")
    base = dict(
        ticker_id=ticker.id, daily_return=0.05, abs_return=0.05, direction="up",
        prev_adj_close=1, adj_close=1, threshold=0.02, threshold_source="floor",
        detector_k=2, detector_window=20, detector_floor=0.02,
    )
    cases = [
        (NewsStatus.PENDING, 0, now + timedelta(days=1)),
        (NewsStatus.COMPLETE, 1, now - timedelta(days=1)),
        (NewsStatus.PARTIAL, 1, now + timedelta(hours=1)),
        (NewsStatus.PARTIAL, 1, now - timedelta(hours=1)),
        (NewsStatus.FAILED, 2, now - timedelta(days=1)),
        (NewsStatus.FAILED, 3, now - timedelta(days=1)),
    ]
    for i, (status, attempts, closes) in enumerate(cases):
        session.add(
            Movement(
                **base, date=date(2026, 1, 1) + timedelta(days=i),
                news_status=status, news_attempts=attempts, news_window_closes_at=closes,
            )
        )
    await session.commit()

    movements = (await session.scalars(sa.select(Movement).order_by(Movement.date))).all()
    expected = [m.id for m in movements if ingestion.needs_enrichment(m, now)]
    selected = await ingestion.enrichable_movements(session, ticker, limit=100, now=now)

    assert sorted(m.id for m in selected) == sorted(expected)
    assert len(expected) == 3  # pending, closed partial, failed under the cap


# ---------------------------------------------------------- refresh chunk


async def test_a_ticker_being_ingested_by_a_user_is_skipped(db, monkeypatch):
    install_feed(monkeypatch, {"BUSY": build_price_history("BUSY")})
    run = await new_run(db)
    async with db() as session:
        ticker = await ingestion.get_or_create_ticker(session, "BUSY")
        assert await ingestion.claim_ingestion(session, ticker)

        outcome = await prewarm.refresh_chunk(
            session, [entry("BUSY")], run.id, now=datetime.now(timezone.utc)
        )

    assert outcome.skipped_claimed == 1 and outcome.refreshed == 0
    assert await all_of(db, Movement) == []
    assert await all_of(db, Job) == []


async def test_new_tickers_get_a_full_year_and_known_ones_the_short_window(db, monkeypatch):
    monkeypatch.setattr(settings, "prewarm_price_lookback_days", 45)
    monkeypatch.setattr(settings, "price_history_days", 365)
    run = await new_run(db)
    async with db() as session:  # KNOWN already has recent bars and a profile
        known = await ingestion.get_or_create_ticker(session, "KNOWN")
        await ingestion.refresh_prices(session, known, history_ending_on(TODAY - timedelta(days=2), "KNOWN"))
        await session.commit()
    feed = install_feed(
        monkeypatch,
        {"KNOWN": history_ending_on(TODAY, "KNOWN"), "NEW": history_ending_on(TODAY, "NEW")},
    )

    async with db() as session:
        await prewarm.refresh_chunk(
            session, [entry("KNOWN"), entry("NEW")], run.id, now=datetime.now(timezone.utc)
        )

    assert sorted(feed.calls) == [(["KNOWN"], 45, []), (["NEW"], 365, ["NEW"])]


async def test_a_movement_past_its_attempt_cap_is_not_queued_again(db, monkeypatch):
    monkeypatch.setattr(settings, "news_max_attempts", 3)
    install_feed(monkeypatch, {"TIRED": build_price_history("TIRED")})
    async with db() as session:
        ticker = await ingestion.get_or_create_ticker(session, "TIRED")
        (movement,) = (
            await ingestion.refresh_prices(session, ticker, build_price_history("TIRED"))
        ).created
        movement.news_status = NewsStatus.FAILED
        movement.news_attempts = 3
        await session.commit()
    run = await new_run(db)

    async with db() as session:
        outcome = await prewarm.refresh_chunk(
            session, [entry("TIRED")], run.id, now=datetime.now(timezone.utc)
        )

    assert outcome.refreshed == 1 and outcome.enrichments_queued == 0
    assert await all_of(db, Job, Job.kind == JobKind.ENRICH_MOVEMENT) == []


async def test_one_bad_symbol_does_not_fail_the_chunk_but_all_bad_does(db, monkeypatch):
    install_feed(
        monkeypatch,
        {"GOOD": build_price_history("GOOD"), "FLAKY": PriceDataError("yfinance", "timeout")},
    )
    run = await new_run(db)
    async with db() as session:
        outcome = await prewarm.refresh_chunk(
            session, [entry("GOOD"), entry("FLAKY"), entry("GONE")], run.id,
            now=datetime.now(timezone.utc),
        )
        assert (outcome.refreshed, outcome.failed, outcome.not_found) == (1, 1, 1)

        gone = await ingestion.get_ticker(session, "GONE")
        assert gone.ingest_status == IngestStatus.FAILED and gone.ingest_error_permanent
        flaky = await ingestion.get_ticker(session, "FLAKY")
        assert not flaky.ingest_error_permanent

        with pytest.raises(PriceDataError):
            await prewarm.refresh_chunk(
                session, [entry("FLAKY")], run.id, now=datetime.now(timezone.utc)
            )


# ------------------------------------------------------------------ budget


async def enrich_job(db, movement_id: int, run_id: int | None, priority: int = 40) -> Job:
    async with db() as session:
        payload = {"movement_id": movement_id}
        if run_id is not None:
            payload["run_id"] = run_id
        job = await queue.enqueue(
            session, JobKind.ENRICH_MOVEMENT, payload, priority=priority,
            dedupe_key=queue.enrich_key(movement_id), source=JobSource.SCHEDULED,
            run_id=run_id,
        )
        await session.commit()
        return job


async def pending_movements(db, *symbols: str) -> list[Movement]:
    out = []
    async with db() as session:
        for symbol in symbols:
            ticker = await ingestion.get_or_create_ticker(session, symbol)
            out += (
                await ingestion.refresh_prices(session, ticker, build_price_history(symbol))
            ).created
        await session.commit()
    return out


async def test_enrichment_past_the_budget_is_deferred(db, worker, stub_llm):
    first, second = await pending_movements(db, "AAA", "BBB")
    run = await new_run(db, budget=1)
    await enrich_job(db, first.id, run.id, priority=30)
    await enrich_job(db, second.id, run.id, priority=40)

    await drain(worker)

    async with db() as session:
        assert (await session.get(Movement, first.id)).news_status == NewsStatus.COMPLETE
        assert (await session.get(Movement, second.id)).news_status == NewsStatus.PENDING
        stored = await session.get(PrewarmRun, run.id)
        assert (stored.enrichments_used, stored.enrichments_deferred) == (1, 1)
    deferred = await all_of(db, Job, Job.dedupe_key == queue.enrich_key(second.id))
    assert deferred[0].status == JobStatus.SUCCEEDED
    assert deferred[0].progress == {"outcome": "deferred"}


async def test_a_retried_enrichment_does_not_pay_twice(db, worker, monkeypatch):
    (movement,) = await pending_movements(db, "AAA")
    run = await new_run(db, budget=5)
    job = await enrich_job(db, movement.id, run.id)
    async with db() as session:
        claimed = await queue.claim(session, "w")
        assert await queue.take_enrichment_budget(session, run.id, job=claimed)
        await queue.fail(session, claimed, PriceDataError("x", "blip"), now=datetime.now(timezone.utc) - timedelta(days=1))

    await drain(worker)

    async with db() as session:
        assert (await session.get(PrewarmRun, run.id)).enrichments_used == 1
        assert (await session.get(Job, job.id)).status == JobStatus.SUCCEEDED


async def test_interactive_and_followup_enrichment_never_pay(db, worker):
    bumped, followup = await pending_movements(db, "AAA", "BBB")
    run = await new_run(db, budget=0)
    await enrich_job(db, bumped.id, run.id, priority=40)
    await enrich_job(db, bumped.id, None, priority=queue.PRIORITY_INTERACTIVE)  # a user asked
    await enrich_job(db, followup.id, None, priority=queue.PRIORITY_FOLLOWUP)

    await drain(worker)

    async with db() as session:
        for movement in (bumped, followup):
            assert (await session.get(Movement, movement.id)).news_status == NewsStatus.COMPLETE
        assert (await session.get(PrewarmRun, run.id)).enrichments_used == 0


# ---------------------------------------------------------------- scheduler


async def test_the_scheduler_enqueues_the_latest_run_once(db, worker):
    now = datetime(2026, 7, 7, 22, 0, tzinfo=timezone.utc)  # Tuesday, after 17:15 EDT

    job = await worker.schedule_tick(now)
    again = await worker.schedule_tick(now + timedelta(minutes=5))

    assert job.kind == JobKind.SCHEDULE_NIGHTLY
    assert job.dedupe_key == "nightly:2026-07-07"
    assert job.payload == {"trading_date": "2026-07-07"}
    assert again.id == job.id, "deduplicated while queued"
    due = datetime(2026, 7, 7, 21, 15, tzinfo=timezone.utc)
    jitter = timedelta(seconds=settings.prewarm_jitter_seconds)
    assert due - jitter <= job.run_after.replace(tzinfo=timezone.utc) <= due + jitter


async def test_a_date_that_already_ran_is_not_scheduled_again(db, worker):
    async with db() as session:
        await prewarm.start_run(session, date(2026, 7, 7), now=datetime.now(timezone.utc))
        await session.commit()

    assert await worker.schedule_tick(datetime(2026, 7, 7, 22, 0, tzinfo=timezone.utc)) is None


async def test_a_second_nightly_job_for_one_date_does_nothing(db, worker, monkeypatch):
    install_feed(monkeypatch, {})
    for _ in range(2):  # two replicas, the first job already finished
        async with db() as session:
            await queue.enqueue(
                session, JobKind.SCHEDULE_NIGHTLY, {"trading_date": TODAY.isoformat()},
                priority=5, dedupe_key=queue.nightly_key(TODAY), source=JobSource.SCHEDULED,
            )
            await session.commit()
        await drain(worker)

    assert len(await all_of(db, PrewarmRun)) == 1


# --------------------------------------------------------- the whole night


async def test_one_night_end_to_end(db, worker, monkeypatch):
    """Four tickers: two popular (one moved long ago, one today), one seed no
    one has asked for, and one that does not exist. Budget for two."""
    monkeypatch.setattr(settings, "prewarm_seed_symbols", "SEED")
    monkeypatch.setattr(settings, "prewarm_max_enrichments_per_run", 2)
    install_feed(
        monkeypatch,
        {
            "HOT": build_price_history("HOT"),  # a 2024 move: window long closed
            "SEED": build_price_history("SEED"),  # same sector, same 2024 date
            "TODAY": history_ending_on(TODAY, "TODAY"),  # window still open
        },
    )
    macro_searches: list[str] = []
    original = FixtureNewsProvider.execute

    async def counting(self, request):
        if request.query.startswith("macroeconomic"):
            macro_searches.append(request.summary_query)
        return await original(self, request)

    monkeypatch.setattr(FixtureNewsProvider, "execute", counting)
    async with db() as session:
        for symbol, hits in (("HOT", 5), ("TODAY", 2), ("NOPE", 1)):
            for _ in range(hits):
                await demand.record_demand(session, symbol)
        await session.commit()
        await queue.enqueue(
            session, JobKind.SCHEDULE_NIGHTLY, {"trading_date": TODAY.isoformat()},
            priority=5, dedupe_key=queue.nightly_key(TODAY), source=JobSource.SCHEDULED,
        )
        await session.commit()

    ran = await drain(worker)

    kinds = [job.kind for job in ran]
    assert kinds[:2] == [JobKind.SCHEDULE_NIGHTLY, JobKind.REFRESH_PRICES]
    # Every enrichment ran after the macro search it was blocked on.
    position = {job.dedupe_key: i for i, job in enumerate(ran)}
    enrichments = [job for job in ran if job.kind == JobKind.ENRICH_MOVEMENT]
    assert len(enrichments) == 3
    for job in enrichments:
        assert position[job.blocked_by_key] < position[job.dedupe_key]
    # Two sectors-and-dates, two macro searches, for three enrichments.
    assert len(macro_searches) == 2

    async with db() as session:
        status = {}
        for symbol in ("HOT", "SEED", "TODAY"):
            ticker = await ingestion.get_ticker(session, symbol)
            (movement,) = (
                await session.scalars(sa.select(Movement).where(Movement.ticker_id == ticker.id))
            ).all()
            status[symbol] = movement.news_status
        # The seed nobody asked for had the lowest priority: it lost the budget.
        assert status == {
            "HOT": NewsStatus.COMPLETE,
            "TODAY": NewsStatus.PARTIAL,
            "SEED": NewsStatus.PENDING,
        }
        nope = await ingestion.get_ticker(session, "NOPE")
        assert nope.ingest_error_permanent

        run = (await session.scalars(sa.select(PrewarmRun))).one()
        assert run.status == PrewarmRunStatus.FINISHED
        assert (run.universe_size, run.movements_found, run.enrichments_queued) == (4, 3, 3)
        assert (run.enrichments_used, run.enrichments_deferred) == (2, 1)

    # TODAY's PARTIAL pass left a follow-up for when its window closes.
    (followup,) = await all_of(db, Job, Job.source == JobSource.FOLLOWUP)
    assert followup.status == JobStatus.QUEUED
    assert followup.run_after.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)

    # A user asking for the popular ticker now gets it at once, with news.
    async with await signed_in(db) as client:
        body = (await client.get("/tickers/HOT")).json()
        assert body["status"] == "ready" and body["job_id"] is None
        assert body["movements"][0]["news"]
        # The deferred one is enriched on demand, ahead of anything nightly.
        seed = (await client.get("/tickers/SEED")).json()
        assert seed["status"] == "refreshing"
    job = (await all_of(db, Job, Job.id == seed["job_id"]))[0]
    assert job.priority == queue.PRIORITY_INTERACTIVE


async def signed_in(db) -> AsyncClient:
    """A client for the API on `db`, called as a signed-in user."""
    _, key = await new_api_key(db)
    return api_client(api_app(db, StubLLM()), key)


# ------------------------------------------------- enrichment on request


async def test_a_users_request_pulls_a_queued_nightly_enrichment_forward(db, monkeypatch):
    (movement,) = await pending_movements(db, "LATE")
    async with db() as session:
        ticker = await ingestion.get_ticker(session, "LATE")
        ticker.ingest_status = IngestStatus.COMPLETE
        ticker.last_ingested_at = datetime.now(timezone.utc)
        await session.commit()
    run = await new_run(db)
    nightly = await enrich_job(db, movement.id, run.id, priority=67)

    async with await signed_in(db) as client:
        body = (await client.get("/tickers/LATE")).json()

    assert body["status"] == "refreshing"
    assert body["job_id"] == nightly.id, "the queued job is reused, not duplicated"
    job = (await all_of(db, Job, Job.id == nightly.id))[0]
    assert job.priority == queue.PRIORITY_INTERACTIVE


async def test_wait_true_enriches_what_is_owed_inline(db):
    (movement,) = await pending_movements(db, "INLINE")
    async with db() as session:
        ticker = await ingestion.get_ticker(session, "INLINE")
        ticker.ingest_status = IngestStatus.COMPLETE
        ticker.last_ingested_at = datetime.now(timezone.utc)
        await session.commit()

    async with await signed_in(db) as client:
        body = (await client.get("/tickers/INLINE", params={"wait": True})).json()

    assert body["status"] == "ready"
    assert body["movements"][0]["news_status"] == "complete"
    assert body["movements"][0]["news"]


# ------------------------------------------------------------------- admin


async def test_admin_endpoints_are_off_without_a_token(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", None)
    response = await client.post("/admin/prewarm")
    assert response.status_code == 503
    assert "ADMIN_TOKEN" in response.json()["detail"]


async def test_admin_endpoints_reject_a_wrong_token(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "s3cret")
    assert (await client.get("/admin/queue")).status_code == 401
    assert (await client.get("/admin/queue", headers={"X-Admin-Token": "nope"})).status_code == 401


async def test_admin_can_trigger_a_run_once_per_date(client, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "s3cret")
    headers = {"X-Admin-Token": "s3cret"}

    response = await client.post("/admin/prewarm", params={"trading_date": "2026-07-07"}, headers=headers)
    assert response.status_code == 202
    body = response.json()
    assert body["trading_date"] == "2026-07-07"

    async with session_factory() as session:
        job = await session.get(Job, body["job_id"])
        assert job.kind == JobKind.SCHEDULE_NIGHTLY and job.dedupe_key == "nightly:2026-07-07"
        await prewarm.start_run(session, date(2026, 7, 7), now=datetime.now(timezone.utc))
        await session.commit()

    again = await client.post("/admin/prewarm", params={"trading_date": "2026-07-07"}, headers=headers)
    assert again.status_code == 409


async def test_admin_queue_summary(client, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "s3cret")
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        run = await prewarm.start_run(session, date(2026, 7, 7), now=now - timedelta(minutes=5))
        run.universe_size = 40
        for key in ("a", "b"):
            await queue.enqueue(
                session, JobKind.ENRICH_MOVEMENT, {}, priority=40, dedupe_key=key,
                source=JobSource.SCHEDULED, run_after=now - timedelta(minutes=3), max_attempts=1,
            )
        await session.commit()
        doomed = await queue.claim(session, "w")
        await queue.fail(session, doomed, TickerNotFoundError("ZZZ"))

    body = (await client.get("/admin/queue", headers={"X-Admin-Token": "s3cret"})).json()

    counts = {(c["kind"], c["status"]): c["count"] for c in body["counts"]}
    assert counts == {("enrich_movement", "queued"): 1, ("enrich_movement", "dead"): 1}
    assert body["oldest_queued_age_seconds"] >= 170
    assert body["dead"][0]["dedupe_key"] == "a"
    assert "TickerNotFoundError" in body["dead"][0]["last_error"]
    assert body["last_run"]["trading_date"] == "2026-07-07"
    assert body["last_run"]["universe_size"] == 40
