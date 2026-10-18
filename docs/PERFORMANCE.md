# Performance: the read path under a market-open spike

All numbers here are **synthetic-local**: one machine, synthetic data, a fake
model. They compare Metrix before and after Phase 4's caching on identical
inputs; they say nothing absolute about production's hardware, network or row
counts. Production was never load tested.

## What changed in Phase 4

- **`GET /tickers/{symbol}` validates and caches.** Every committed write that
  changes what the endpoint shows increments the ticker's `data_version`
  (`app/db/versions.py`). The ETag hashes that version, the query, the request's
  freshness decision, the evidence jobs' state and the response schema, so a
  `304` needs no response to be built. The same tag keys a Redis response cache,
  invalidated by construction. `Cache-Control: private, max-age=60,
  stale-while-revalidate=300` on settled data. The web app sends
  `cache: "no-cache"`, so it revalidates every time (cheap) instead of trusting a
  copy that may predate an update.
- **Live updates replace the 3.5 s job poll** (`GET /events`, server-sent events
  from Postgres `LISTEN`), so a user waiting on an ingestion no longer adds a
  request every 3.5 s.
- **Symbol search** has a pattern-ops index on the symbol and pg_trgm indexes on
  names: at 20,000 directory rows the query went from a 12.7 ms sequential scan
  to a 0.34 ms BitmapOr over both indexes (`EXPLAIN ANALYZE`, local Postgres 16).

## The test

`loadtest/locustfile.py` (Locust 2.37.10, `loadtest/requirements.txt`): each
simulated user opens one of five hot tickers (NVDA, AAPL, TSLA, MSFT, AMZN) as the
web app does (`include_prices=true&limit=50`), keeps reading them (switching
ticker 30% of the time), and asks Ask Metrix a question in about 1 of 20 actions.
Users wait 0.5 to 2 s between actions and arrive at 10 per second. Each run is
60 s.

- **Data:** `scripts/bench_read_path.py seed` into a throwaway database (5
  tickers, 115 movements, 345 links, through the real pipeline). Warm: every
  ticker answers `ready` with no job before a run starts (the runner checks).
- **Model:** `loadtest/fake_openai.py`, an OpenAI-compatible stand-in that
  answers after exactly 1.0 s. Chat therefore measures Metrix's own overhead
  around a model of known latency.
- **Server:** one uvicorn worker (as production runs it), local Postgres 16 and
  Redis 7, `AUTH_REQUIRED=false`. Every simulated user shares one IP, so the
  anonymous plan's request and chat quotas were raised (the quota is still
  charged on every request), as was `OPENAI_MAX_RPM` (see below).
- **Before** is the Phase 3 tip (`5ca1659`), run from a worktree on the same
  database; **after** is this branch. "reval" means users send `If-None-Match`
  as a browser does; "noreval" measures the Redis cache alone.
- **Machine:** 2 vCPUs (Intel Xeon @ 2.80 GHz), shared by the server, Postgres,
  Redis and Locust itself, which flatters neither side.

Raw Locust output: `docs/v2/loadtest/*_stats.csv` and `*_failures.csv`.

## Results

`GET /tickers/[hot]` (milliseconds; throughput in requests per second):

| Run | Users | Requests | Errors | p50 | p95 | p99 | Throughput |
|---|---:|---:|---:|---:|---:|---:|---:|
| before | 50 | 1,554 | 0.0% | 490 | 790 | 950 | 26.3 |
| after, reval | 50 | 2,098 | 0.0% | 16 | 51 | 180 | 35.5 |
| after, noreval | 50 | 2,074 | 0.0% | 16 | 56 | 180 | 35.1 |
| before | 150 | 991 | 3.2% | 1,900 | 28,000 | 30,000 | 16.8 |
| after, reval | 150 | 5,155 | 0.0% | 120 | 360 | 580 | 87.3 |

`POST /chat` (with the 1.0 s fake model):

| Run | Users | Requests | Errors | p50 | p95 | p99 |
|---|---:|---:|---:|---:|---:|---:|
| before | 50 | 63 | 0.0% | 2,000 | 2,500 | 3,200 |
| after, reval | 50 | 102 | 0.0% | 1,100 | 1,100 | 1,300 |
| before | 150 | 38 | 7.9% | 11,000 | 37,000 | 41,000 |
| after, reval | 150 | 248 | 0.0% | 1,400 | 1,800 | 2,000 |

What the numbers say:

- **Before, the single worker is saturated at 50 users**: about 26 reads a
  second, each building the full response (bars, movements, links, articles).
  Throughput at 50 users is offered load, not capacity: the users wait between
  actions.
- **At 150 users the old read path collapses.** Requests queue for database
  connections until the pool's 30 s timeout (`QueuePool limit of size 5 overflow
  10 reached`, 38 times in the server log), giving 500s and a 28 s p95. Chat
  slows down with it, because it shares the worker and the pool.
- **After, a warm read costs about 16 ms** at 50 users, whether it ends in a 304
  or a Redis hit: what remains is the work every request still does (auth, the
  per-minute quota, counting demand, the freshness check, the version and
  evidence-job reads). At 150 users the same worker serves 87 reads a second with
  no errors and a 360 ms p95, and chat stays within 0.4 to 0.8 s of the model's
  own 1.0 s.
- **The two after variants are close** because building the body was the cost;
  revalidation additionally saves the bytes (a 304 has no body, against tens of
  kilobytes with a year of prices).

## A limit the test found: provider RPM caps chat

With the default `OPENAI_MAX_RPM=50` (the account's limit, shared across
processes), the first run at 50 users answered **53% of questions with 503**:
interactive calls wait at most 5 s for a token (`rate_limit_wait_exceeded`), then
give up. That is the rate limiter working as designed, and it is the real
ceiling for questions at a spike: about 50 a minute per OpenAI account, whatever
the API's capacity. The fix is a higher account tier (and setting
`OPENAI_MAX_RPM` to match), not code. The runs above lift it to measure Metrix
itself.

## Reproducing

```bash
# Postgres on 5433 and Redis on 6380 (docker compose up -d db redis, or local)
createdb -h 127.0.0.1 -p 5433 -U metrix metrix_bench_p4
export DATABASE_URL=postgresql+asyncpg://metrix:metrix@127.0.0.1:5433/metrix_bench_p4
alembic upgrade head
NEWS_PROVIDER=fixture python scripts/bench_read_path.py seed

# The fake model, and the API with the load-test environment
uvicorn loadtest.fake_openai:app --port 8099 &
REDIS_URL=redis://127.0.0.1:6380/3 NEWS_PROVIDER=fixture AUTH_REQUIRED=false \
PLAN_LIMITS_JSON='{"anonymous": {"requests_per_minute": 100000000, "chat_per_minute": 100000000, "chat_per_day": 100000000}}' \
OPENAI_API_KEY=fake OPENAI_BASE_URL=http://127.0.0.1:8099/v1 OPENAI_MAX_RPM=1000000 DAILY_SPEND_CAP_USD=1000000 \
uvicorn app.main:app --port 8000 --workers 1 --no-access-log &

# The spike (in its own virtualenv: pip install -r loadtest/requirements.txt)
REVALIDATE=1 locust -f loadtest/locustfile.py --headless -u 150 -r 10 -t 60s \
  --host http://127.0.0.1:8000 --csv out/after-u150-reval
```

For "before", check out `5ca1659` in a worktree and start its API the same way
on the same database. Never point the load test at a deployment.
