# Metrix: Stock Movement News Explainer

Takes a ticker, finds the days it moved unusually far, and explains each one with the
news that caused it — company news, competitor and industry news, and macro/political
news — with an LLM deciding which articles actually explain the move and why.

```
GET  /tickers/{symbol}   stock + news data, nested movement → articles → tier + rationale
POST /chat               grounded, multi-turn Q&A over that data, with citations
GET  /jobs/{id}          status, progress and errors of a queued background job
GET  /health             liveness and which integrations are configured
```

---

## Quick start

```bash
git clone https://github.com/eungi-hong/metrix.git && cd metrix
cp .env.example .env          # then add your two API keys (below)
docker compose up --build     # Postgres + migrations + API + worker
```

The API is on <http://localhost:8000>, interactive docs at <http://localhost:8000/docs>.

> The `curl` examples below print raw JSON. For the same data as a readable tree,
> skip to [Reading it in a terminal](#reading-it-in-a-terminal).

### API keys

| Variable | Required | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | <https://console.anthropic.com/settings/keys> |
| `EXA_API_KEY` | Yes (or use `NEWS_PROVIDER=fixture`) | <https://dashboard.exa.ai/api-keys> |

Without an Exa key, set `NEWS_PROVIDER=fixture` to run the entire pipeline offline
against deterministic synthetic articles. Without an Anthropic key the price and
movement-detection half still works — movements come back with
`news_status: "failed"` and a warning explaining why, rather than a 500.

### Running without Docker

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
docker compose up -d db
alembic upgrade head
uvicorn app.main:app --reload
python -m app.worker           # in a second terminal: runs queued ingestion
```

Without the worker, `?wait=true` requests still work, but ingestion that is not
waited on stays queued.

> Use **Python 3.12 or 3.13** (3.12 is what the image is built on). The pinned
> `greenlet` and `pydantic-core` ship no wheels for 3.14 and do not compile against it,
> so a venv made with a bare `python3` will fail to install if that is your 3.14.
> Skip `docker compose up -d db` if you would rather point `DATABASE_URL` at a Postgres
> of your own.

> The compose Postgres publishes **5433** on the host (not 5432) so it does not collide
> with a local Postgres. `.env.example` already points at 5433.

---

## Using it

### Fetch a ticker

The first request for a ticker has nothing stored, so it triggers ingestion. Use
`wait=true` to run it inline and get the finished data back in one call:

```bash
curl "http://localhost:8000/tickers/NVDA?wait=true&limit=3"
```

Without `wait`, you get `202` immediately and ingestion is queued for the worker. The
response carries a `job_id`; asking again while it is queued joins the same job
rather than starting another:

```bash
curl "http://localhost:8000/tickers/NVDA"      # 202, status: "ingesting", job_id: 17
curl "http://localhost:8000/jobs/17"           # status, attempts, progress, last_error
curl "http://localhost:8000/tickers/NVDA"      # 200, status: "ready" once finished
```

A job that fails on something transient (a timeout, a 5xx, a rate limit) is retried
with backoff; one that fails on something permanent (an unknown symbol, a missing or
rejected API key) is marked `dead` at once. The job queue, the worker and the reasons
behind them are described in [docs/PREWARMING.md](docs/PREWARMING.md).

Response shape — news is nested inside the movement it explains, not returned as a
second flat list you have to join:

```jsonc
{
  "status": "ready",
  "ticker": { "symbol": "NVDA", "company_name": "NVIDIA Corporation",
              "sector": "Technology", "industry": "Semiconductors" },
  "price_range": { "start": "2025-09-11", "end": "2026-09-10", "bars": 251 },
  "pagination": { "limit": 50, "offset": 0, "total": 24, "returned": 3 },
  "movements": [
    {
      "date": "2026-08-27",
      "daily_return_pct": 8.74,
      "direction": "up",
      "threshold": 0.0419,              // the bar this day had to clear
      "threshold_source": "volatility", // which term of the max() bound it
      "rolling_std": 0.0210,
      "sigma_multiple": 4.17,
      "detector_k": 2.0, "detector_window": 20, "detector_floor": 0.02,
      "news_status": "complete",
      "news": [
        {
          "relevance_tier": "easy",
          "relevance_score": 0.93,
          "rationale": "Q2 datacenter revenue beat consensus by 12%, reported after the prior close.",
          "search_tier": "easy",
          "article": { "title": "...", "url": "https://...",
                       "source": "reuters.com", "published_at": "2026-08-26T21:04:00Z" }
        }
      ]
    }
  ],
  "warnings": []
}
```

#### Filters

| Parameter | Meaning |
|---|---|
| `start`, `end` | Date range (`YYYY-MM-DD`) |
| `min_magnitude_pct` | Minimum move size **in percent** — `5` means 5% |
| `direction` | `up` or `down` |
| `tier` | `easy`, `medium`, `hard`. Repeat for several. Restricts both which movements are returned and which articles are shown under them |
| `include_prices` | Include the daily OHLCV bars themselves, not just the summary. Honours `start`/`end`. Off by default — a year is ~250 rows most callers don't need |
| `limit`, `offset` | Pagination over movements (`total` is the unpaginated count) |
| `refresh` | Force re-ingestion even if data is fresh |
| `wait` | Run any needed ingestion inline instead of queueing it for the worker |

```bash
# Big down days in 2026 that macro or political news helps explain
curl "http://localhost:8000/tickers/NVDA?start=2026-01-01&direction=down&min_magnitude_pct=4&tier=hard"

# Second page of everything explained by company-specific or industry news
curl "http://localhost:8000/tickers/NVDA?tier=easy&tier=medium&limit=10&offset=10"
```

```bash
# The price series, for charting
curl "http://localhost:8000/tickers/NVDA?include_prices=true&limit=0" | jq '.prices[:3]'
```

### Chat

```bash
curl -X POST http://localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"ticker": "NVDA", "question": "What drove the biggest drop this year?"}'
```

```jsonc
{
  "conversation_id": "6f1c...",
  "ticker": "NVDA",
  "answer": "The largest decline was -6.20% on 2026-06-05 [M3], attributed to ...[A2]",
  "grounded": true,
  "sources": {
    "movements": [{ "ref": "M3", "movement_id": 12, "date": "2026-06-05", ... }],
    "articles":  [{ "ref": "A2", "article_id": 44, "url": "https://...", ... }]
  }
}
```

Multi-turn — pass `conversation_id` back, and the ticker carries over:

```bash
curl -X POST http://localhost:8000/chat -H 'content-type: application/json' \
  -d '{"conversation_id": "6f1c...", "question": "Was that company news or macro?"}'
```

The answer cites `[M3]`/`[A2]` labels that map to the `sources` block, so every claim
traces back to a row in the database. If nothing relevant is stored, `grounded` is
`false` and the model is instructed to say so rather than speculate.

---

## Reading it in a terminal

The JSON is nested three levels deep — movement, then the articles explaining it,
then each article's tier, score and rationale — which is the point of the product and
also unreadable as raw output. `scripts/show.py` renders the same payload as a tree.

It is a client that runs on *your* machine, not inside the container, so install its
one dependency locally first — a one-time step if you started with Docker:

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install httpx
```

With the venv activated, run it as:

```bash
scripts/show.py DHI                                    # movements + their news
scripts/show.py DHI --tier hard                        # macro explanations only
scripts/show.py DHI --direction down --min-pct 4
scripts/show.py NVDA --wait                            # ingest inline if cold
scripts/show.py DHI --ask "what drove the biggest drop?"
```

```
D.R. Horton, Inc.  DHI
Consumer Cyclical · Residential Construction · NYSE · USD
251 bars  2025-09-11 → 2026-09-10  █▆▆▇▃▄▄▃▂▁▂▅▃▄▂▂▄▄▃▄▅▆▅▃▁▁▁▂  178.86 → 135.57
ready · 22 movement(s), showing 3

  2026-06-11  ▲ +5.26%  2.4σ  bar 4.40% (volatility)
    ├─ medium 0.55  prnewswire.com · 2026-06-11
    │  Lennar Reports Second Quarter 2026 Results
    │  → Lennar's Q2 results would be read as a positive read-through for peer
    │    DHI, published on T+0 during the move.
    └─ hard   0.45  apnews.com · 2026-06-11
       US stocks jump, and oil prices ease on hopes for a deal...
       → Broad market rally (S&P 500 +1.8%) driven by Iran deal hopes and easing
         oil prices, which could explain part of DHI's move as a tailwind.
```


### Flags

`scripts/show.py --help` prints these too.

| Flag | Does |
|---|---|
| `TICKER` | Required, positional. e.g. `DHI` |
| `--ask QUESTION` | Ask `POST /chat` instead of fetching movements |
| `--conversation ID` | Continue a previous conversation (id is printed by `--ask`) |
| `--start DATE` / `--end DATE` | Only movements in this window |
| `--tier easy\|medium\|hard` | Filter by relevance tier. Repeat for several |
| `--direction up\|down` | Only up days or only down days |
| `--min-pct N` | Only movements at least this large, in percent |
| `--limit N` | Movements to return (default 10) |
| `--wait` | Ingest inline if the ticker is cold — slow, but returns finished data |
| `--refresh` | Force re-ingestion even if data is fresh |
| `--no-sparkline` | Skip fetching prices (a year of bars is ~250 rows) |
| `--json` | Print the raw API payload instead of the tree |
| `--color auto\|always\|never` | Default `auto`: on for a terminal, off when piped |
| `--base-url URL` | Default `$METRIX_URL`, else `http://localhost:8000` |
| `--timeout SECONDS` | HTTP timeout (default 900, generous for `--wait`) |

Colour is on for a terminal and off when piped, and it is never the only signal —
direction also carries a glyph and a sign, tiers are spelled out, scores are numbers.
So piped output loses emphasis but no information:

```bash
scripts/show.py DHI | less        # no escape sequences
NO_COLOR=1 scripts/show.py DHI
scripts/show.py DHI --json | jq   # raw payload passthrough
```

---

## How a movement is defined

A trading day is a **major movement** when

```
|daily_return| >= max(floor, k * rolling_std)
```

- `daily_return` is computed from **adjusted** close, so splits and dividends do not
  masquerade as movements.
- `rolling_std` is the sample standard deviation of the `k`-window daily returns
  **immediately preceding** the day being tested.
- Defaults: `MOVEMENT_STD_WINDOW=20`, `MOVEMENT_K=2.0`, `MOVEMENT_FLOOR_PCT=0.02`.
  All three are environment variables, and the values in force are stored on every
  movement row and returned in the API response.

**Why both terms.** A flat 2% rule is wrong in both directions. On NVDA it flags 95 of
251 trading days — 38% of the year, which is not a useful definition of "major". On a
quiet utility the same 2% may be a genuine three-sigma event. The volatility term
adapts the bar to each stock's own regime; the floor stops a very low-volatility name
from being flagged on noise when 2σ is a few basis points. Measured over the last year:

| Ticker | Trading days | Flagged by flat 2% | Flagged by `max(2%, 2σ)` |
|---|---|---|---|
| NVDA | 251 | 95 | **24** |
| KO | 251 | 22 | **15** |
| JNJ | 251 | 28 | **15** |

**The window excludes the day being tested.** Including it would let a large move
inflate its own threshold and mask itself — a lookahead bias that makes the most
interesting days the least likely to be flagged. Every threshold is computable from
information available at the prior close, and there is a test asserting exactly that
(`test_threshold_is_computable_from_information_available_at_the_prior_close`).

**It is auditable.** Each movement stores `rolling_std`, `threshold`,
`threshold_source` (`floor` or `volatility`), and the `k`/window/floor used. You can
see not just that a day was flagged, but which term of the `max()` decided it.

---

## How news relevance works

### Three searches, then one judgement

For each movement, three searches run against Exa over a date window around the
movement (`T-3` to `T+1` by default):

| Tier | Query asks | Example |
|---|---|---|
| **Easy** | What happened at this company? | earnings, guidance, launches, lawsuits, filings, analyst actions |
| **Medium** | What happened to its competitors or industry? | a peer's results, sector pricing, supply/demand shifts |
| **Hard** | What happened to the market? | rate decisions, inflation data, tariffs, regulation, geopolitics |

The Hard-tier search deliberately **names no company** — only the sector. That is what
makes the tier work at all (a Fed decision article never mentions NVIDIA), and it means
every ticker in a sector on a given date produces an identical request and shares one
cached search. "Identical" covers every field the cache key hashes, including the
question Exa summarizes each result against: the Easy and Medium summaries ask about
this company's move, the Hard summary only asks what would move the sector.

Competitors for the Medium tier are resolved by asking the LLM, grounded in the
yfinance `sector`/`industry`, and cached on the ticker row for 30 days. A static
ticker→competitor map was the alternative; it is accurate the day it is written, goes
stale in a quarter, and covers only the tickers someone thought to add — which would
silently reduce the Medium tier to nothing for most inputs. If the LLM is unavailable
the tier degrades to an industry-keyword search rather than disappearing.

### The scoring step is the point

A date-windowed search returns everything published near the move, and most of it is
noise. Keyword overlap and recency do not establish explanation. So every deduplicated
candidate goes into **one LLM call per movement**, along with the move's size and
direction, and is scored on:

1. **Causality** — does it report something that moves a share price, or is it just
   *about* the company? Recaps that cite the price move as their subject explain nothing.
2. **Direction** — is the news consistent with the *sign* of the move?
3. **Timing** — publication dates are pre-computed relative to the movement (`T-1`,
   `T+0`) so the model reasons about timing instead of subtracting dates.

The model returns a score (0–1), a tier, and a one-line rationale, all stored on the
`movement_news_links` row and returned in the API. Articles scoring below
`RELEVANCE_MIN_SCORE` (0.35) or judged irrelevant are not linked at all.

Scoring all candidates in one call — rather than per article or per tier — lets the
model rank them against each other and **reassign the tier**: the link stores both
`search_tier` (which query found it) and `relevance_tier` (what the model says it
actually is), because a macro-flavoured search regularly surfaces a company-specific
story and vice versa.

### Worked example: all three tiers on one ticker

Real output for `DHI` (D.R. Horton, a rate-sensitive homebuilder):

```
2026-06-24  +6.68%
   [medium 0.92] T+0  Homebuilder Stocks Rise on Bipartisan Housing Bill ...
   [easy   0.90] T+0  D.R. Horton and Lennar Stocks Trade Up ...
2026-06-11  +5.26%
   [hard   0.55] T+0  US stocks jump, and oil prices ease on hopes for a deal ...
        → "Broad market rally (S&P 500 +1.8%) on T+0 driven by Iran deal hopes and
           easing oil prices, which would reduce inflation fears and rate expectations"
2026-04-21  +5.78%
   [easy   0.92] T+0  D.R. Horton Reports Fiscal 2026 Second Quarter Earnings
   [easy   0.72] T+0  RBC Capital raises D.R. Horton price target on earnings
```

The Hard-tier article **never mentions D.R. Horton**. It was found by a company-free
macro query and connected to the move through rate sensitivity — which is the entire
point of the tier, and what keyword search over the ticker cannot do.

### A failure this design caught

An early live run linked an article published `2026-07-30` to a movement on
`2026-07-02` and the scorer rated it **0.95** — a confident explanation for a move
four weeks earlier. Two causes: Exa treats the published-date filter as a hint and
leaked an out-of-range result, and deduplication later resolved a dateless search hit
to a stored article whose real date was outside the window. The window is now enforced
in code at both points — on provider output, and against the stored article before a
link is written — rather than trusted to the provider or the model. Both paths have
regression tests.

---

## Architecture

```
app/
  api/routes/     tickers.py, chat.py, jobs.py, health.py — thin: validate, delegate, shape
  api/errors.py   domain exception → HTTP status, one error body shape
  services/
    movements.py  volatility-adjusted detection — pure, no DB, no network
    prices.py     yfinance, defensive; single and batched fetches, and the merge
                  that rebases stored history after splits and dividends
    news/         provider abstraction, Exa adapter, fixture adapter, response cache,
                  tier query construction
    peers.py      competitor resolution (LLM, cached on the ticker)
    relevance.py  the scoring prompt and its structured output
    ingestion.py  orchestration: refresh_prices (bars → movements), then
                  enrich_movement (news → scores → links) per movement
    queue.py      the Postgres job queue: enqueue/dedupe, claim, retry, reap
    demand.py     decayed popularity per symbol, and the nightly pre-warm universe
    job_handlers.py  what each kind of queued job does
    chat.py       retrieval, prompt assembly, citation labels
    llm/          provider abstraction, Anthropic adapter, uniform error mapping
  models/         SQLAlchemy: tickers, price_history, movements, news_articles,
                  movement_news_links, news_query_cache, conversations, chat_messages,
                  jobs, prewarm_runs, ticker_demand
  worker.py       `python -m app.worker`: claim loops, heartbeats, graceful shutdown
  schemas/        Pydantic request/response models
  core/           config (all tunables), logging, domain exceptions
```

Ingestion is a service, not route code: `GET /tickers/{symbol}` decides *whether*
ingestion is owed, and `app.services.ingestion` decides what that means.

### Idempotency

Re-running for a ticker extends and corrects; it never duplicates.

- Price bars and movements are reconciled against what is stored — insert new, update
  changed (adjusted closes get revised by splits), delete days that no longer clear the
  threshold.
- Articles deduplicate on a **normalized**-URL hash (lowercased host, `www.` and
  tracking parameters stripped), so the same story found by two different tier searches
  becomes one row.
- Movement→article links carry a uniqueness constraint.
- A movement whose news enrichment is `complete` is skipped, so a re-run costs no
  LLM calls for work already done.
- A movement enriched before its news window closed (the `T+1` day, plus
  `NEWS_WINDOW_GRACE_HOURS`) is `partial`, not `complete`: articles published later in
  the window may be missing. It is enriched again once the window has closed, and that
  pass re-scores the fuller candidate set and replaces the earlier verdict.
- A movement whose enrichment keeps failing stops being retried automatically after
  `NEWS_MAX_ATTEMPTS` (default 3) attempts. `?refresh=true` retries it anyway.

### Failure policy

Prices are load-bearing: if yfinance fails there is nothing to explain, and the request
returns `404` (unknown ticker) or `502` (upstream failure). Everything downstream is
best-effort **per movement** — a news timeout or an LLM error marks that one movement
`news_status: "failed"`, adds a warning to the response, and the run continues. One
flaky news search never costs the caller its price and movement data.

| Condition | Status |
|---|---|
| Unknown/delisted ticker | `404 not_found` |
| Invalid symbol, bad date range | `422` |
| Cold ticker, ingestion queued | `202` + `status: "ingesting"` + `job_id` |
| Last ingestion failed, nothing stored | `502` + `status: "failed"` and the reason |
| News API or LLM failed | `502 upstream_unavailable`, or a per-movement warning |
| Missing API key | `503 configuration_error`, naming the variable |

A ticker whose ingestion failed reports `status: "failed"` with the underlying error
rather than silently retrying on every poll. `?refresh=true` forces a retry.

### Cost and rate-limit control

Ingesting a ticker with 24 movements naively would be 72 searches and hundreds of LLM
calls. What keeps it bounded:

- **Raw provider responses are cached in Postgres** (`news_query_cache`). Once a date
  window has closed its answer never changes, so the entry is kept for 90 days
  (`NEWS_CACHE_CLOSED_WINDOW_TTL_DAYS`); while it is still open, for 2 hours
  (`NEWS_CACHE_OPEN_WINDOW_TTL_HOURS`) and never past the moment it closes.
  Re-running during development is free, and sector-mates share their macro searches.
- **One LLM scoring call per movement**, not per article or per tier.
- **`MAX_MOVEMENTS_PER_INGEST`** (default 10) caps enrichment per run, largest moves
  first. The response warns how many were skipped; re-running picks up the next batch.
- **Peers are cached** for 30 days — one call per ticker, not per movement.
- Exa calls retry with exponential backoff on 429/5xx only; a 4xx is not retried,
  because retrying a malformed request just burns quota.

### Concurrency

Two simultaneous requests for a cold ticker must produce one ingestion, not two. The
lock is a single atomic conditional `UPDATE` on the ticker row, which is correct on any
database with transactions, needs no advisory-lock support, and self-heals if the
holding process dies (a claim older than 15 minutes can be re-taken).

Queued jobs are guarded the same way one level up: at most one queued or running job
per piece of work (a partial unique index on `jobs.dedupe_key`), and workers claim
with `FOR UPDATE SKIP LOCKED`, so no two take the same job. The worker's
`ingest_ticker` job still takes the ticker claim, so a queued run and a `wait=true`
run never write the same ticker at once.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

No Docker, no network, no API keys.

- `tests/test_movements.py` — 35 tests. The detection math: the floor, the sigma
  term, their interaction, the no-lookahead guarantee, direction symmetry, degenerate
  inputs (empty series, flat series, duplicate dates, non-positive prices), and
  parameter validation. Plus the yfinance error classification that decides whether a
  failed fetch is an unknown symbol (404) or a broken upstream (502).
- `tests/test_api.py` — a smoke test per endpoint plus filtering, pagination, the
  202/200 ingestion states, idempotent re-ingestion, the concurrency claim, error
  mapping, and multi-turn chat.
- `tests/test_news.py` — window enforcement, URL-normalized deduplication, the
  read-through response cache, its window-aware TTL, and Hard-tier cache sharing.
- `tests/test_freshness.py` — PARTIAL enrichment, idempotent re-enrichment, and the
  retry cap on movements that keep failing.
- `tests/test_queue.py` — the queue's semantics: dedupe and priority bumps, claim
  order, blocking, backoff, permanent errors, stale locks and heartbeats, the
  enrichment budget.
- `tests/test_worker.py` — the worker and job handlers end to end, including
  graceful shutdown and the follow-up job a PARTIAL enrichment enqueues.
- `tests/test_prices.py` — batched yfinance parsing from a synthetic multi-ticker
  frame, merging a short refresh window into stored history, and the split/dividend
  rebase.
- `tests/test_relevance.py` — the scoring request and result, built and applied
  without the model in between.
- `tests/test_demand.py` — popularity decay, recording hits, and choosing the pre-warm
  universe (seeds, most popular, recently requested, minus permanent failures).
- `tests/test_show_cli.py` — the renderer's citation parsing, colour gating, and
  sparkline edge cases.

Endpoint tests run on in-memory SQLite with the news provider and LLM stubbed, so
`pytest` is one command with no dependencies. The models are declared portably
(`JSONB` on Postgres, `JSON` elsewhere) to make that possible.

The queue claims jobs with a Postgres-only statement, so its tests also run against
real Postgres when `TEST_DATABASE_URL` is set, along with `tests/test_postgres.py`
(concurrent claimers through `SKIP LOCKED`, concurrent budget spending, `NOTIFY`).
The database named there is wiped, and must have `test` in its name:

```bash
docker compose up -d db
docker compose exec db createdb -U metrix metrix_test
TEST_DATABASE_URL=postgresql+asyncpg://metrix:metrix@localhost:5433/metrix_test pytest
```

---

## Known limitations

- **Movement detection is univariate.** It does not separate a stock's own move from
  its sector's or the market's. A beta-adjusted or residual-return definition would
  distinguish "NVDA fell because NVDA" from "NVDA fell because everything fell", which
  is exactly the distinction the Hard tier is trying to make downstream.
- **Article bodies are whatever Exa returns** — usually a summary or the first ~2k
  characters, sometimes paywalled boilerplate. Scoring quality is bounded by that.
- **No backfill scheduler yet.** Ingestion runs through a durable job queue, but is
  still request-triggered: the first caller for a ticker waits. Scheduled pre-warming
  is in progress; see [docs/PREWARMING.md](docs/PREWARMING.md).
- **Relevance is unevaluated.** There is no labelled set, so "the scoring is good" is
  an assertion, not a measurement. See `SUBMISSION.md`.
