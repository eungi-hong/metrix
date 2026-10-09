# Pre-warming

Pre-warming computes the data users are likely to ask for before they ask, so that
popular tickers are already warm. This document grows with the work. The fixes below
came first, because both undermined pre-warming: one made the cheapest shared search
unshared, the other made early results permanent.

## Fixes that came first

### The Hard-tier cache was not shared across tickers

The Hard-tier search is meant to be company-free, so that every ticker in a sector on
a given date produces the same request and they all share one `news_query_cache` row.
The query text was company-free. The request was not. `build_tier_queries` attached
one `summary_query` to all three tiers, and that summary named the company and the
size of its move ("Does this article explain why NVIDIA stock moved -4.10% on
2026-06-05?"). `NewsSearchRequest.cache_fingerprint` hashes every field of the request,
so the Hard-tier key differed per ticker and the sharing described in the code, the
README and the submission write-up never happened. Each ticker paid for its own macro
search.

The Hard tier now has its own summary question, which depends only on sector and date:
"Which macroeconomic, political or regulatory developments in this article would move
{sector} stocks around {date}?" Easy and Medium keep the company-specific summary. A
test ingests two differently named companies in the same sector and asserts exactly one
upstream Hard-tier call.

This has a cost. Exa writes each result's summary against the summary question, and
the scorer reads that summary. For macro articles it now reads a sector-level summary
instead of one written for this company's move. The scorer is still told the company,
the size and the direction of the move in its own prompt, and deciding whether a macro
event explains this particular move was always its job, so the loss is a less targeted
snippet, not lost information. The gain is one macro search per sector per day instead
of one per ticker, which is the property the nightly run depends on.

Two workers can now miss on the same cache key at the same moment and both insert it.
The insert runs inside a savepoint, and a unique-constraint violation is logged and
treated as a cache hit, so the losing worker's transaction survives.

### Recent movements were marked complete before their news window closed

A movement's news window runs from three days before the move to one day after it.
Enrichment set `news_status = complete` as soon as it ran, and re-ingestion skips
complete movements. A move enriched on the evening it happened therefore never picked
up the next day's articles, which are often the ones that explain it. The response
cache had the same blind spot: it used a flat 24-hour TTL on the grounds that a past
window's answer never changes, which is only true once the window has closed.

Each movement now stores `news_window_closes_at`, the window's last instant plus
`NEWS_WINDOW_GRACE_HOURS` (default 6) for indexing lag. Enrichment that runs before
then marks the movement `partial`; enrichment after it marks it `complete`. A partial
movement is not re-enriched while its window is still open, and is re-enriched as soon
as it has closed. Today that happens on the next ingestion of the ticker. Once the job
queue exists, the partial pass will enqueue its own follow-up to run at
`news_window_closes_at`.

Re-enrichment re-scores the whole candidate set and replaces the previous verdict.
Articles and links are upserted, so nothing is duplicated, and a link the new pass no
longer supports is removed rather than left with its old score. If the second search
comes back empty, the earlier links are kept, since finding no evidence is not evidence
against them.

The cache TTL now follows the same rule. A response for a closed window is kept for
`NEWS_CACHE_CLOSED_WINDOW_TTL_DAYS` (90). A response for an open window is kept for
`NEWS_CACHE_OPEN_WINDOW_TTL_HOURS` (2), and never past the moment the window closes, so
the first search after closing gets the final answer.

Each enrichment attempt also increments `news_attempts`. A movement that has failed
`NEWS_MAX_ATTEMPTS` times (default 3) is no longer retried automatically; `refresh=true`
still retries it. Without this, a movement whose news can never be fetched would cost a
search and a scoring call on every run.

The migration backfills `news_window_closes_at` and moves rows the old code got wrong,
complete movements whose `news_fetched_at` is earlier than their window close, back to
`partial`, so they are picked up again. `news_status` is a VARCHAR with no CHECK
constraint, so the new value needed no DDL.

## The job queue

All background work now goes through a job queue in Postgres, including the on-demand
ingestion that used to run in FastAPI's `BackgroundTasks`. `BackgroundTasks` ran inside
the API process: work was lost on restart, nothing bounded how much ran at once,
nothing showed what was running, and nothing retried a failure.

The queue is a `jobs` table rather than Redis or Celery. It adds no infrastructure, and
a job can be enqueued in the same transaction as the data that made it necessary: when
an enrichment marks a movement PARTIAL, the movement's new status and its follow-up job
commit together or not at all. What it gives up is throughput, which does not matter
here. External rate limits cap the useful rate at a few jobs per second.

The code is in `app/services/queue.py`, with the handlers in
`app/services/job_handlers.py` and the worker in `app/worker.py`.

### Enqueueing and deduplication

Every job has a `dedupe_key` that names the work, such as `ingest:NVDA` or `enrich:42`.
A partial unique index allows at most one queued or running job per key. Enqueueing a
key that already has a queued job adds nothing; it lowers that job's priority number to
the smaller of the two and moves its `run_after` to the earlier of the two. That is how a
user asking for a ticker already queued for tonight makes it run now. A key whose job
is already running returns that job unchanged. Finished jobs keep their keys as history
and do not block new work.

The follow-up for a PARTIAL movement uses its own key, `enrich:{id}:followup`. It is
enqueued from inside the running `enrich:{id}` job, and under that same key the dedupe
would return the running job and the follow-up would never exist.

Two enqueuers racing on one key both see no active job and both insert; the unique
index rejects the second, which then finds and returns the first. The insert runs in a
savepoint, so the rejected one does not abort the caller's transaction.

### Claiming

Lower priority numbers run first, then the earliest `run_after`, then the oldest job.
On Postgres a claim is a single statement:
`UPDATE jobs SET status = 'running', … WHERE id = (SELECT id … ORDER BY priority,
run_after, id LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id`. `SKIP LOCKED` makes
concurrent claimers step over rows another transaction already has, instead of waiting
on them, so N workers take N different jobs. SQLite, used by the test suite, has no row
locks, so there the claim picks a candidate and then takes it with an UPDATE that only
succeeds if the job is still queued. This is the same pattern
`ingestion.claim_ingestion` uses. The queue tests run against both databases when
Postgres is available, because the two claims are different statements and must behave
the same.

A job is not claimable before its `run_after`, or while an active job holds its
`blocked_by_key`. Any terminal state of the blocker releases its dependents, including
`dead`. A dependent then does the blocked work itself, which is slower but never hangs.

### Failure, retries and locks

A failed attempt goes back to `queued` with a later `run_after`: the base delay doubles
per attempt, up to a ceiling, and is scaled by a random factor of ±25% so jobs that
failed together do not all retry in the same second. After `max_attempts` the job is
`dead`. Errors that cannot succeed on a retry go straight to `dead`. These are an
unknown symbol, a missing key, and a key the provider rejects. They are marked with a
`permanent` flag on the domain exception rather than recognised by their message text.

A worker that dies leaves its jobs `running`. Each worker reaps jobs whose lock has not
been refreshed within `JOB_LOCK_TIMEOUT_MINUTES` and requeues them. The orphaned
attempt counts, so a job that crashes its worker every time it runs ends up `dead`
instead of cycling forever. While a job runs, the worker refreshes its lock every
`JOB_HEARTBEAT_SECONDS`, so a slow but healthy job is never reaped and run twice. Every
transition after the claim is a conditional UPDATE that also matches `locked_by`. If a
job was reaped and claimed by another worker while the first was still running it, the
first worker's late result changes nothing.

The status list includes `failed` alongside `dead`, but nothing writes `failed` at the
moment: both permanent errors and exhausted retries end as `dead`. Anything that
treats terminal states, such as unblocking dependents, treats the two alike.

Every status or progress change sends `NOTIFY job_events, '<job id>'` on Postgres in the
same transaction, so a listener only hears about committed changes. Nothing listens
yet. It is groundwork for a live frontend.

### The worker

`python -m app.worker`, or the `worker` service in `docker-compose.yml`, runs
`WORKER_CONCURRENCY` claim loops in one process. Each loop runs one job at a time in its
own session and sleeps for a jittered poll interval when nothing is due. Workers only
claim the job kinds they have handlers for, so kinds added by later stages wait in the
queue until a worker that knows them is deployed.

Priority alone does not stop a user from waiting. If every loop is busy with a
30-second nightly enrichment, a cold ticker is first in line but still waits for one to
finish. `WORKER_INTERACTIVE_SLOTS` of the loops therefore only claim priority-0 jobs,
the ones a user is waiting on, and sit idle otherwise. The other loops claim anything.

Handlers report progress, such as `{"stage": "enriching", "done": 4, "total": 10}`, by
recording it on their context. The handler never writes it to the database itself. A
per-job keepalive task writes the latest value along with the lock heartbeat, so
reporting progress can never block, slow or fail a handler. The final value is saved
in the same transaction that marks the job succeeded.

On SIGTERM the loops stop claiming, and in-flight jobs get
`WORKER_SHUTDOWN_TIMEOUT_SECONDS` to finish. Jobs still running after that are
cancelled and released back to the queue with their attempt refunded, so a deploy never
costs a job one of its retries. Compose gives the worker a longer `stop_grace_period`
than that timeout.

### Two writers, one ticker

The ticker claim (`ingestion.claim_ingestion`) is still the only thing that stops two
runs from writing one ticker's bars and movements at once. The API no longer takes it
when it enqueues, because a job can sit in the queue for longer than a claim stays
valid. The `ingest_ticker` handler takes it when it runs. If a `wait=true` request is
already ingesting that ticker inline, the job succeeds without doing anything, since
that request is doing the same work. An `enrich_movement` job writes only its own
movement and needs no ticker claim. If the movement has been deleted by the time the
job runs, because a price refresh re-ran detection and dropped it, the job succeeds as
a no-op.

### The enrichment budget

`prewarm_runs` holds one row per nightly run, including that run's enrichment budget.
A unit is taken with one conditional UPDATE,
`… SET enrichments_used = enrichments_used + 1 WHERE enrichments_used <
enrichment_budget`, which commits at once. Parallel spenders therefore cannot overspend,
and nobody holds the run's row lock while enriching. A run is finished when it has no
queued or running jobs left. Each job checks this after committing its own final
state, so when the last two jobs finish at the same moment, at least one of them sees
the other as done.
