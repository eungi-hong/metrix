# Fairness and cost protection

Phase 1 made Metrix fast for many users. This phase makes it safe for them. Before it,
the API was open and several paths let any caller spend money or degrade service for
everyone else. The order of priorities is: protect the budget, then fairness between
users, then latency. This document grows with the work, and starts with the budget,
because everything else sits on top of it.

## The spend ledger and the daily cap

Before this stage, cost was logged (`exa_search.cost_usd`, `llm_call` tokens) but never
added up, and nothing stopped a bad night or a busy afternoon from spending without
limit.

Every billable external call, an Exa search or an LLM call, now goes through
`spend.metered` in the provider seam itself (`app/services/news/exa.py`,
`app/services/llm/anthropic.py`), so no call site can forget it. A news cache hit never
reaches the seam, so it costs and records nothing. Each call leaves one row in
`usage_events`: provider, operation (`search`, `relevance`, `peers`, `chat`), model,
token counts, cost, whether the cost is an estimate, and who it was for. Exa's cost is
its own `costDollars.total`; when Exa omits it, `EXA_COST_ESTIMATE_USD` is recorded and
flagged. An LLM call is priced from `LLM_PRICES_JSON`, which ships commented out with a
placeholder, because prices change and a guessed price inside a cap is worse than none.
A model without a price is recorded at a deliberately pessimistic fallback rate, flagged,
and announced at startup with `llm_model_unpriced`.

Who a call is for comes from three context variables (`app/core/context.py`), not new
parameters through every service. The API marks each request `interactive`. The worker
marks each job by its priority: `interactive` at priority 0, where a user is waiting,
and `background` otherwise, including a nightly job a user's request has pulled
forward. Code outside both, such as a script, defaults to `background`, the stricter
class.

`DAILY_SPEND_CAP_USD` bounds a UTC day's spend. Background calls may use at most
`BACKGROUND_SPEND_SHARE` of it (0.6 by default), which reserves the rest for users: a
heavy nightly run can never leave them with nothing. The cap is required when
`APP_ENV=prod`; elsewhere an unset cap means unlimited, with `spend_cap_unset` logged
at startup. `spend_threshold_crossed` is logged once a day when settled spend reaches
`SPEND_ALERT_FRACTION` of the cap, and `spend_cap_reached` once a day per class when the
first call is refused.

### Reserve, then settle

The brief proposed checking the cap before each call and recording the cost after.
That leaves a race: N concurrent calls all read the same headroom and all proceed, so the
cap can be overshot by N times the largest call. Instead, `spend.reserve` sets the
call's estimated cost aside on the day's `spend_daily` row in one conditional UPDATE,
the pattern of the nightly enrichment budget: it succeeds only if spent plus reserved
plus the estimate still fits under the cap (and under background's share, for a
background call). The database serializes those UPDATEs, so concurrent calls cannot
jointly overspend; a Postgres test races forty reservations for one cap and exactly the
number that fit get through. After the call, `settle` writes the ledger row and moves
the amount from reserved to spent. A call that fails releases its reservation and
records nothing.

What is left is estimate error. The cap can be overshot only by the amount actual costs
exceed their estimates, summed over the calls in flight when it is reached. Estimates
are built high: an LLM call is estimated at its full `max_tokens` of output and at one
input token per two characters of prompt (real text is closer to four), and an Exa
search at `EXA_COST_ESTIMATE_USD`. For the LLM the overshoot is therefore zero; for Exa
it is at most (searches in flight) times the amount a search's real cost exceeds the
configured estimate.

A process that dies between reserving and settling leaves its reservation on the row
until the day ends. That overcounts, the safe direction for a cap, and `GET
/admin/usage` shows reservations separately so a leak is visible.

### What a refusal does

`SpendCapReached` is deliberately not a `MetrixError`. The pipeline catches
`MetrixError` to degrade gracefully, recording a failed search or scoring pass against
the movement and moving on; a cap recorded that way would use up the movement's attempts
and mark it FAILED. Instead the refusal travels to the boundary, and each caller does
the right thing with it.

In the worker, the job is held, not failed (`queue.hold`): it goes back to the queue
with `run_after` at the next 00:00 UTC plus up to `SPEND_RESUME_JITTER_SECONDS`, gets
its attempt back, and is marked `hold_reason = "spend_cap"`. Its movement is left as it
was, normally PENDING. A nightly enrichment held this way counts in its run's
`enrichments_deferred`. Because the nightly run starts at 21:15–22:15 UTC, a run that
reaches its share resumes on the next day's budget about two hours later. An ingestion
stopped by the cap keeps the prices it fetched and finishes the ticker; only the news
waits.

In the API, chat answers 503 with `error: "spend_cap_reached"` and `Retry-After` set to
the seconds until midnight UTC, and nothing of the turn is stored. `GET
/tickers/{symbol}` never errors on the cap: with `wait=true` it serves what is stored
with a warning naming what was not fetched, and without it, once a user call has been
refused today, any response that queued work warns that the work will run after
midnight UTC.

Reserving needs the database. If Postgres cannot be reached the reservation raises and
the call is not made: money fails closed. Settling is best-effort, like recording
demand: a ledger write that fails is logged as `spend_record_failed` and never fails the
call it records, which has already been paid for.

### Seeing it

`GET /admin/usage?date=YYYY-MM-DD` (default today, UTC) gives total, interactive and
background spend; spend and call counts by provider and operation, with how many costs
were estimates; the top ten users by spend, once users exist; the cap and background
cap; what is reserved by calls in flight; and the headroom left for each class.
`GET /admin/queue` now also counts the jobs held by the cap.

## Identity

Before this stage anyone could call anything, and a conversation belonged to nobody:
whoever held its id could read or continue it. Quotas, fair shares and the top-users
view of the ledger all need to know who is calling.

Identity is API keys we issue, not logins or an OAuth provider: enough to attribute
spend, enforce per-user quotas and own conversations. One dependency,
`CurrentUser` in `app/api/deps.py`, decides who a request is from, so a later move to
JWT or OAuth changes that function and nothing that depends on it.

A key is `mtx_` and 32 random bytes. Only its SHA-256 is stored. A fast hash is right
for a random 256-bit token, where it would be wrong for a password: a slow hash exists
to make guessing expensive, and nothing guesses 256 random bits, so bcrypt would add
latency to every request and no security. The key is found by its prefix and its hash
compared with `secrets.compare_digest`. The brief asked for the first eight characters
as the prefix, but four of those are `mtx_`, leaving 24 random bits, which would
collide within a few thousand keys. The prefix is therefore `mtx_` plus eight random
characters (48 bits), and issuing a key simply draws again on the rare collision. An
unknown key, a revoked key and a disabled user all get the same 401, so the response
reveals nothing about which keys exist; `auth_failed` logs the reason, without any of
the key. `last_used_at` is written at most once per `API_KEY_TOUCH_INTERVAL_MINUTES`,
so authenticating is not a write on every request.

With `AUTH_REQUIRED=false` a caller without a key becomes an anonymous principal
keyed by client IP, on the `anonymous` plan; a key that is sent is still checked. The
IP is the socket peer. `X-Forwarded-For` is trusted only when `TRUSTED_PROXY_COUNT` is
set, and then only the entry the outermost trusted proxy wrote: everything to its left
the client could have written itself.

A conversation is owned by the caller who started it. Reading or continuing anyone
else's answers 404, not 403, so its existence is not revealed, and an unknown id gets
the same 404. (It used to start a new conversation silently.) Conversations from before
this stage have no owner and are readable only with the admin token.

A job is visible at `GET /jobs/{id}` to every caller whose request it serves, recorded
in `job_requesters`. A column on the job would not do: deduplication folds a second
user's request into the first user's job and hands both of them its id. The job's own
`user_id` is the user whose request created it, and the worker attributes its spend
to them. Nightly jobs serve no request and are admin-only.

## Quotas

Identity makes per-caller limits possible; quotas are those limits. Each plan
(`anonymous`, `free`, `pro`, `internal`, from `PLAN_LIMITS_JSON`) sets
`requests_per_minute` for every authenticated call, `chat_per_minute` and
`chat_per_day` for chat turns, `cold_ingests_per_day` for requests that start new
ingestion or news work, `refresh_per_day` for `refresh=true` requests that start work,
and `allow_wait`. Every count is per principal: a user, or an anonymous caller's IP.

### Why Redis for limits, and Postgres for spend and the queue

Redis comes in for exactly the counters that are hot, short-lived and approximate. A
quota is checked on every request, and its state matters for a minute or a day. In
Postgres that would be a write on every request, against rows every request of a busy
user contends on. In Redis it is one round trip to a script that runs atomically.

Money is the opposite case. Spend has to be durable and auditable, so the ledger and
the cap stay in Postgres, and the cap must hold when Redis is down; it does, because
Redis is never consulted for it. The queue stays in Postgres too: a job commits in
the same transaction as the data that made it necessary, which Redis cannot offer.

### The algorithms

Per-minute limits use GCRA, the generic cell rate algorithm. It keeps one number per
key, the time at which the key would be fully rested, and moves it forward by
`period / limit` per request; a request that would push it more than one period ahead
is refused. That gives a burst of up to `limit`, then exactly the sustained rate. A
sliding-window log would store a timestamp per request and trim and count them on
every check; GCRA is one key and one number in constant memory, one atomic Lua script,
and it knows exactly when the next request will fit, so `Retry-After` is exact. The
script reads the time from Redis, so processes whose clocks disagree still agree on
the limit.

Daily quotas are a counter on a key named for the UTC date, expiring an hour after
midnight, checked and incremented in one script so a refused request costs nothing.

### Charging and refunding

A quota is charged before the work, so concurrent requests cannot all slip past it,
and given back if the work then fails for a reason that is ours. A chat turn whose
model call fails, or which the spend cap refuses, is refunded; a user should not lose
quota to our outage. A turn the caller got wrong, such as an id for someone else's
conversation, keeps its charge, so quota cannot be spent probing for free.

Cold ingests are charged only when new work actually starts. A request that joins an
ingestion already queued (by anyone) costs nothing, and if two requests race to queue
the same ticker, the one whose job turns out to exist already is refunded. Fetching
news still owed on an otherwise warm ticker is real Exa and LLM work, so a request that
starts any of it costs one cold ingest, however many movements it covers. A warm
ticker with nothing owed never counts. Out of cold ingests, a ticker with stored data is
served from storage with a warning; one with nothing stored gets the 429. An explicit
`refresh=true` past `refresh_per_day` is always a 429, because the caller asked for
exactly that work. A queued job that later fails in the worker is not refunded: it is
retried, and its work is usually done in the end.

`wait=true` runs work inside the API process, bypassing the queue. It is allowed only
on plans with `allow_wait`; elsewhere the request is queued instead, with a warning,
rather than refused, so a client that always sends it still works. Where it is
allowed, a process-wide semaphore of `API_MAX_INLINE_INGESTIONS` bounds how many run
at once, and a request that finds every slot busy gets 429 at once rather than
waiting, since waiting would tie up the API exactly as running would.

### When Redis is down

Per-user limits fail open, but stay bounded. On a Redis error the limiter logs
`limiter_fallback` (at most once a minute) and uses a per-process in-memory store for
`REDIS_RETRY_SECONDS`, then tries Redis again, so an outage costs one short timeout
rather than one per request. Each process then enforces each limit on its own, so a
caller can get up to (processes x limit) for the length of the outage. Taking the API
down because a limiter is unavailable would be worse, and the spend cap, in Postgres,
still holds. `/health` reports `"redis": "unreachable"` and status `degraded`.

## Abuse controls on the expensive paths

### Unknown symbols cost nothing

Before this stage the only check on a symbol was a regex, so any well-formed nonsense
(`QWZX`) cost a yfinance call, a failed ingestion and a demand hit. Now a symbol must be
in the US symbol directory, table `listed_symbols`, before anything external is called
for it. Otherwise `GET /tickers` answers 404 at once, records no demand, and logs
`symbol_rejected`. Chat needs no check: it only ever looks up tickers already stored.

The directory comes from Nasdaq Trader's two pipe-delimited files,
`nasdaqlisted.txt` (Nasdaq) and `otherlisted.txt` (NYSE, NYSE American, NYSE Arca, Cboe,
IEX and others), checked against the live files when this was written: a header line,
one row per symbol, a `File Creation Time` trailer, and rows flagged `Test Issue = Y`
that are not real listings. The parser is tested against samples cut from those files.
A `refresh_symbol_directory` job replaces the table once per ISO week, scheduled by the
worker like the nightly run (dedupe key `symbols:2026-W41`).

A refresh must never leave the service worse off. A failed download, a file without
its trailer (cut short), fewer than `SYMBOL_DIRECTORY_MIN_ROWS` symbols, or a drop of
more than `SYMBOL_DIRECTORY_MAX_SHRINK` of the current table each raise, leaving the
table as it was. The job is then retried with backoff. Only then is the table
replaced, in one transaction, so readers see the old directory or the new, never half
of one. A week whose refresh fails every attempt keeps last week's directory.

The files write class shares with a dot and preferreds with a dollar sign (`BRK.B`,
`ABR$D`); Yahoo, which ingestion fetches from, writes `BRK-B` and `ABR-PD`. The
directory stores Yahoo's form, and a request in the files' form is served under it, so
`BRK.B` and `BRK-B` are the same ticker.

The directory is US-only. Foreign listings (`RY.TO`, `VOD.L`) are allowed through
`SYMBOL_ALLOWLIST`. Tickers already ingested successfully, which may since have been
delisted, and the seed list are always allowed. `SYMBOL_DIRECTORY_MODE` is `enforce`,
`warn` (serve, and log what would have been refused) or `off`. `enforce` acts as `warn`
until the first refresh fills the table, so a fresh deploy works on day one.

### Demand one caller cannot manufacture

Demand decides what the nightly run spends money on. Before this stage every request
counted, so one script requesting `XYZ` in a loop would push it into the nightly top N,
and the nightly budget would be spent on it.

Now a caller's requests for a symbol count at most once per UTC day, through a
`SET NX` in the limiter's store. Twenty-five requests from one caller and one from
another are two hits, and a test shows one caller's spam losing a place in the universe
to three real users. While Redis is down the check runs per process; that is slightly
more generous than shared, and bounded, which the brief's "always count" would not have
been. Anonymous callers count at `DEMAND_ANONYMOUS_WEIGHT` (0.25), since they are cheap
to multiply. The internal plan counts at `DEMAND_INTERNAL_WEIGHT` (0), since our own
traffic is not demand. A request refused by the directory, or answered 429, records no
demand at all; a request for a real ticker that then fails still does.

### refresh=true

A refresh is charged against `refresh_per_day` when it starts work. On top of that,
whoever asks, a ticker is refreshed at most once per `REFRESH_COOLDOWN_MINUTES` (60):
inside the cooldown the stored data is served, with a note saying when a refresh is
next possible, and nothing is charged. A refresh a few minutes after the last would
find the same prices and the same news.
