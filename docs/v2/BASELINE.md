# Metrix v2: Phase 0 baseline

The "before" numbers every later phase is compared against. Each number has the
command that produced it and a label saying where it came from:

- **synthetic-local**: measured on a local machine against synthetic data. Useful
  for comparing before and after on the same machine; says nothing about
  production's absolute latency.
- **checks**: the repository's own test, lint and build commands.
- **production (pending)**: needs the real deployment or real provider keys. The
  command is here; the number is not, until it has been run.

Measured on 2026-10-18 at `main` = `1b50817` (the branch adds scripts, tests and
docs only; no application code changed).

---

## 1. Checks on `main`

Environment: Python 3.13.16 (CI uses 3.12), Node 22.22.0 with npm 10.9.4 (CI uses
the current LTS), PostgreSQL 16.15, Redis 7.0.15.

| Check | Result | Command |
|---|---|---|
| Lint | pass | `ruff check .` (ruff 0.15.22) |
| Backend tests, SQLite only | 527 passed, 48 skipped | `pytest` |
| Backend tests, with Postgres and Redis | 575 passed | `TEST_DATABASE_URL=postgresql+asyncpg://metrix:metrix@127.0.0.1:5433/metrix_test TEST_REDIS_URL=redis://127.0.0.1:6380/15 pytest` |
| Migration heads | 1 (`a8c4e2f6b1d3`) | `alembic heads` |
| Migrations from empty, and model drift | upgrade clean, no drift | `alembic upgrade head && alembic check` |
| Migrations reversible | `downgrade base` then `upgrade head` both clean | `alembic downgrade base && alembic upgrade head` |
| Frontend install, lint, typecheck | pass | `cd frontend && npm ci && npm run lint && npm run typecheck` |
| Frontend unit tests | 14 passed in 4 files | `npm test` |
| Frontend build | pass; one JS chunk of 588.90 kB (170.31 kB gzip), over Vite's 500 kB warning | `VITE_METRIX_API_BASE_URL=https://api.example.invalid npm run build` |
| Frontend advisories | 10 (2 critical, 5 high, 3 moderate), all in dev and build tooling (vitest and tinypool; tailwind 3's chokidar, fast-glob, micromatch and braces chain; postcss-nested); 0 in runtime dependencies | `npm audit` and `npm audit --omit=dev` |

The unmerged branch `origin/fix/openai-credit-exhausted` (`c6498af`, one commit on
top of `main`) also passes: `ruff check .` clean, 576 passed with Postgres and Redis
(one more parametrised case).

The backend suite was also run on Python 3.12.3, CI's minor version, in a separate
virtualenv from `requirements.txt` and `requirements-dev.txt`: `ruff check .` clean,
575 passed with Postgres and Redis.

The brief estimates "about 400 backend tests"; 575 are collected.

---

## 2. Read path: `GET /tickers/{symbol}` (synthetic-local)

The endpoint Phase 4 will cache, timed warm, on a local API over local Postgres and
Redis, with synthetic data. **These are not production numbers.**

### Data

`scripts/bench_read_path.py seed` puts the five production seed tickers through the
real `ingest_ticker` pipeline with a deterministic synthetic price series (a seeded
random walk with fat-tailed jumps, weekdays only), the fixture news provider and a
deterministic stand-in scorer that links three articles per movement:

| | Tickers | Price bars | Movements | Links | Articles | News cache rows |
|---|---|---|---|---|---|---|
| Seeded | 5 | 1,310 | 115 | 345 | 345 | 334 |
| NVDA (the timed ticker) | 1 | 262 | 28 | 84 | | |

Every movement is enriched (`complete`), so a read finds nothing owed and queues
nothing. For scale: the README reports 24 flagged days for the real NVDA over 251
sessions.

### Commands

```bash
# Postgres on 5433 and Redis on 6380, as `docker compose up -d db redis` gives you
createdb -h 127.0.0.1 -p 5433 -U metrix metrix_bench
export DATABASE_URL=postgresql+asyncpg://metrix:metrix@127.0.0.1:5433/metrix_bench
alembic upgrade head
python scripts/bench_read_path.py seed

# The API, in another terminal, started as production starts it (one uvicorn
# worker, no reload):
DATABASE_URL=postgresql+asyncpg://metrix:metrix@127.0.0.1:5433/metrix_bench \
REDIS_URL=redis://127.0.0.1:6380/0 \
NEWS_PROVIDER=fixture \
AUTH_REQUIRED=false \
PLAN_LIMITS_JSON='{"anonymous": {"requests_per_minute": 1000000}}' \
uvicorn app.main:app --host 127.0.0.1 --port 8000

# Five runs, then the summary below
for i in 1 2 3 4 5; do
  python scripts/bench_read_path.py run --base-url http://127.0.0.1:8000 \
    --json-out docs/v2/baseline/read_path_synthetic_local_run$i.json
done
python scripts/bench_read_path.py report docs/v2/baseline/read_path_synthetic_local_run*.json
```

Why that environment: requests carry no key, as the v1 frontend's do.
`AUTH_REQUIRED=false` is what production and `.env.example` set (the code default
answers 401). That serves them on the anonymous plan, which allows 30 requests a
minute; raising only that number keeps the quota charge in Redis on every request,
as in production, without turning the run into 429s. Each results file records the
`X-RateLimit-Limit` the API reported (1,000,000), which shows the override was in
force.

`run` times 50 warm-up requests and then 500 timed requests per series, for two
queries (`default`: no parameters; `frontend`: `include_prices=true&limit=50`, what
the v1 frontend sends) at concurrency 1 and 8. It refuses to start unless the first
response is `ready` with no job and no warning, and writes nothing if any timed
request fails.

### Results

Five runs on 2026-10-18 between 09:29 and 09:38 UTC, at commit `ac9d3e8`, on a
2-vCPU Intel Xeon (2.80 GHz) cloud VM shared by the client, the API, Postgres and
Redis. Median of the five runs, with the range in brackets:

| Query | Concurrency | Runs | p50 ms | p95 ms | p99 ms | Req/s | Body bytes |
|---|---|---|---|---|---|---|---|
| default | 1 | 5 | 37.75 (30.65 to 40.25) | 47.31 (42.64 to 49.93) | 53.82 (46.71 to 59.98) | 26.4 (24.8 to 30.2) | 44849 |
| default | 8 | 5 | 217.33 (193.99 to 254.68) | 342.65 (331.74 to 367.96) | 474.1 (394.65 to 604.98) | 34.3 (30.1 to 38.2) | 44849 |
| frontend | 1 | 5 | 45.37 (39.8 to 50.59) | 63.55 (59.05 to 65.08) | 76.48 (69.38 to 245.19) | 20.4 (19.1 to 22) | 76142 |
| frontend | 8 | 5 | 303.56 (267.34 to 332.84) | 577.31 (524.84 to 631.9) | 670.19 (590.74 to 722.11) | 23.6 (22.1 to 26.6) | 76142 |

Raw results, with provenance and the payload each series timed:
`docs/v2/baseline/read_path_synthetic_local_run1.json` to `_run5.json`.

### How to read it

- **Synthetic-local only.** No TLS, no proxy, no network hop to Postgres, different
  hardware and row counts from Railway. Use these numbers to compare before and
  after, not to describe production.
- **The machine drifts.** Back-to-back runs differed by up to about 1.5x at the same
  settings. Phase 4 should re-run this immediately before its "after" measurement,
  on the same machine, rather than compare against the table above.
- **Concurrency 8 is mostly queueing.** Going from one client to eight raises the
  median throughput by 30% (`default`, 26.4 to 34.3 req/s) and 16% (`frontend`,
  20.4 to 23.6 req/s) while multiplying the median p50 by 5.8 and 6.7: one uvicorn
  worker on two shared vCPUs is close to saturated by a single client, and eight
  mostly wait for each other.
- **The maximum is not the tail.** At concurrency 1 every series' maximum is 307 to
  373 ms while its p99 is 47 to 88 ms (one exception, 245 ms in run 5's `frontend`
  series): a rare stall, not a slow tail. See `max_ms` in the raw files.
- **Redis fallback during concurrency 8.** Runs 1 to 3 each logged one
  `limiter_fallback` event, 23 to 29 s into the run, which is the concurrency-8
  `default` series (the API log for runs 4 and 5 was not kept). With the event loop
  saturated, a Redis reply was not read within `REDIS_TIMEOUT_SECONDS` (0.25 s), and
  for the next 5 s quotas were counted per process instead. Requests in that window
  skip their Redis calls, which slightly flatters that series.

---

## 3. Production numbers (pending)

These need the deployment or real provider keys, which the environment that wrote
this file could not reach. Each command only reads (the chat command makes three
model calls), and writes a JSON file to send back.

| Number | Status | Command |
|---|---|---|
| Movements per seed ticker, and how many have evidence | pending | `python scripts/baseline_remote.py movements --base-url https://<api> --json-out movements.json` |
| Average spend per enrichment, from the spend ledger | pending | `METRIX_ADMIN_TOKEN=... python scripts/baseline_remote.py usage --base-url https://<api> --days 14 --json-out usage.json` |
| Chat latency for three fixed questions, and how answers cite | pending | `python scripts/baseline_remote.py chat --base-url http://localhost:8000 --label local --json-out chat.json` (needs `OPENAI_API_KEY` in `.env` and NVDA ingested), or `--base-url https://<api> --label production` |

Notes on what each measures:

- `movements` sends no API key on purpose. Production serves anonymous callers stored
  data and starts no new work for them (`cold_ingests_per_day` is 0 for the anonymous
  plan in `.env.production.example`), so the read cannot queue paid enrichment; it
  exits non-zero if a response reports a queued job anyway. It counts once per symbol
  as anonymous demand.
- `usage` divides by relevance calls, one per scored enrichment. Exa searches also
  include the nightly sector pre-warm and searches for movements that found nothing,
  so Exa, relevance and peer costs are reported separately next to the total.
- `chat` asks the same three questions every time (in `CHAT_QUESTIONS`) so Phase 5
  can re-ask them, and records for each answer the citation labels in the text
  against the sources returned, and whether the text contains markdown.

---

## 4. The seed universe

- **Production** pre-warms five symbols: NVDA, AAPL, TSLA, MSFT, AMZN
  (`PREWARM_SEED_SYMBOLS` with `PREWARM_SEED_FILE=/dev/null` in
  `.env.production.example`, and the README's live-demo section), with
  `PREWARM_MAX_ENRICHMENTS_PER_RUN=15` a night.
- **Local and default** settings read `data/seed_universe.txt`, 40 symbols, with a
  budget of 300 enrichments a night (`.env.example`).

"Movements per ticker for the seed universe" in section 3 means the production five
unless `--symbols` says otherwise.

---

## 5. Before screenshots

18 screenshots in `docs/assets/before/`: home (first load), ticker loaded,
movements, news for a move with evidence, news for the pending latest move, and Ask
Metrix with one answer, at 1440, 768 and 390 px, from the frontend's demo-fixture
mode:

```bash
cd frontend
export VITE_METRIX_API_BASE_URL=http://127.0.0.1:9
npm run build
npx vite preview --host 127.0.0.1 --port 4173 --strictPort &
cd ..
python scripts/capture_before_screenshots.py --base-url http://127.0.0.1:4173
```

The script also prints whether the price chart rendered at each width: yes at 1440,
no at 768 and 390. The blank chart area in the narrower captures is the application,
not the capture (see the script's docstring). The live demo could not be captured
from the environment that wrote this file.
