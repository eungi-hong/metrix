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
