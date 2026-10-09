# Deployment

Metrix runs as a public demo: the React frontend on Vercel, and the API, the queue
worker, Postgres and Redis on Railway. Production follows `main`. Merging a pull request
into `main` is the only way to release, and nothing deploys until CI is green.

This document is the runbook: how it fits together, how a change reaches production,
the rules that keep separate deploys compatible, and what to do when something goes
wrong.

## Architecture

```
browser ──► Vercel (static Vite build of frontend/)
   │
   └──────► Railway: api ──┬──► Postgres   jobs, data, spend ledger, users and keys
            (FastAPI)       └──► Redis      quotas, shared provider rate limits
            Railway: worker ──► same Postgres and Redis; Exa, Anthropic, Yahoo
```

- **Vercel** serves the built frontend. The API's URL is baked in at build time
  (`VITE_METRIX_API_BASE_URL`), and `vite build` refuses to run without it, so a
  deploy can never silently point visitors at `localhost:8000`.
- **api** is the Docker image with its default command: uvicorn on `$PORT`, which
  Railway sets. It has the only public domain on Railway.
- **worker** is the same image running `python -m app.worker`. It claims queued jobs
  (cold tickers, enrichments, the nightly pre-warm, the weekly symbol-directory
  refresh) and runs the nightly scheduler. It has no public domain.
- **Postgres** is the system of record: the queue and the spend ledger live there, so
  a lost Redis loses nothing but counters.
- **Redis** holds per-user quotas and the account-wide provider rate limits. While it
  is unreachable, both fall back to per-process limits and `limiter_fallback` is
  logged; the API keeps serving.

Two services build the same Dockerfile, each from its own config file in the
repository root, `railway.api.json` and `railway.worker.json`. Settings in those files
override the dashboard, so they are the source of truth.

### What each setting is for

api (`railway.api.json`):

- `preDeployCommand: alembic upgrade head` migrates once per deploy, in a separate
  container, before any new API container starts. If it fails, the deploy stops and
  the previous release keeps serving. Both services set `SKIP_MIGRATIONS=1`, so the
  image's entrypoint never migrates on Railway (it still does under docker compose).
- `healthcheckPath: /health` holds traffic on the old deployment until the new one
  answers. Railway only checks it during a deploy. `/health` answers 200 even when
  degraded, so read its body (`database`, `redis`), not just the status.
- `numReplicas: 1`. Quotas and rate limits are already shared through Redis, so more
  replicas would be safe, but a demo does not need them.
- `drainingSeconds: 30` gives in-flight requests time to finish after SIGTERM.

worker (`railway.worker.json`):

- `startCommand: python -m app.worker`. Railway runs a start command in exec form in
  place of the image's entrypoint, so the worker is PID 1 and receives SIGTERM itself.
- `preDeployCommand: python scripts/wait_for_migrations.py` waits until the database
  is at this release's migration head. The worker and the API deploy at the same time
  from the same commit, and only the API migrates, since two concurrent
  `alembic upgrade` runs would race. Without the wait, a new worker could start
  against the old schema. A database that is ahead (a rollback) counts as ready. If the
  migration never lands, the wait times out, the worker's deploy fails, and the old
  worker keeps running.
- `drainingSeconds: 45` is longer than `WORKER_SHUTDOWN_TIMEOUT_SECONDS` (30): on
  SIGTERM the worker stops claiming, gives in-flight jobs 30 seconds, then releases
  what is left back to the queue with the attempt refunded. Railway sends SIGKILL only
  after the drain window.
- `numReplicas: 1`. More workers are safe (claims use `SKIP LOCKED`, the nightly run
  is deduplicated per date), but the spend cap, not the worker count, is the
  bottleneck for a demo.

Both: `watchPatterns` list exactly what the image is built from (`app/`, `alembic/`,
`data/`, `scripts/`, `requirements.txt`, the Dockerfile and entrypoint, the service's
own config file). A merge that only touches the frontend or the docs does not restart
the backend. **If the Dockerfile starts copying something new, add it to both lists**,
or changes to it will not deploy.

Vercel (`frontend/vercel.json`, plus the project settings): root directory
`frontend`, framework Vite, `npm ci` and `npm run build`, output `dist`, production
branch `main`. Preview deployments use the same production API, allowed through CORS
by `CORS_ALLOWED_ORIGIN_REGEX`.

Every production variable, with what it is for, is in
[`.env.production.example`](../.env.production.example).

## How a change ships

1. Work on a branch and open a pull request into `main`.
2. CI (`.github/workflows/ci.yml`) runs four jobs: **backend** (ruff, then pytest
   against real Postgres and Redis service containers), **migrations** (exactly one
   head, upgrade from an empty database, `alembic check` for a model change without a
   migration, and the worker's pre-deploy wait), **frontend** (lint, typecheck,
   tests, production build) and **docker** (the image Railway will build).
3. Vercel builds a preview of the frontend for the branch and links it on the PR. It
   talks to the production API, so test against it with care: a chat there is a real,
   paid call.
4. Merge. CI runs again on `main`.
5. Railway has **Wait for CI** on for both services: the deploys sit in `WAITING`
   until every workflow on the commit has finished, and are `SKIPPED` if one fails.
   Vercel builds the production deployment at once but, with **Deployment Checks**
   requiring the four CI jobs, only assigns it to the production domain once they
   pass.
6. On Railway, the API's pre-deploy migrates, the worker's waits for it, and each
   service switches over once healthy. Vercel switches over when its checks pass.

So a red CI never reaches users. A CI job renamed in the workflow must be renamed in
Vercel's Deployment Checks too, or Vercel will wait for a check that never comes.

## Rules that keep separate deploys compatible

The frontend, the API and the worker go live at different moments from the same merge,
and a rollback can put any of them back on its previous release. So at every moment,
release N and release N-1 must be able to run side by side.

### Migrations

Migrations run before the new code starts, while the previous release is still
serving. **Every migration must work with the previous release still running.**

- Additive changes only: new tables, new nullable columns, new columns with a server
  default, new indexes (on a large table, `CREATE INDEX CONCURRENTLY` in its own
  migration).
- Never rename or drop in one step. Split it across releases:
  1. *Expand*: add the new column or table. The code writes both old and new and
     reads the old.
  2. Backfill, in the migration or a job.
  3. Switch reads to the new shape, in a later release.
  4. *Contract*: drop the old column, in a release after that, once nothing running
     reads it.
- `NOT NULL` on an existing column: add it nullable, backfill, then constrain in a
  later release.
- Keep each migration's downgrade working, but do not rely on it in production (see
  Rollback).

CI enforces the mechanics (one head, a clean upgrade, models and migrations in step).
Compatibility with the previous release is on the reviewer of the PR.

### API and frontend

The frontend may go live before or after the API. Backend changes to a response must
be **additive**: new fields are fine, but renaming, removing or changing the type of a
field is not, until a frontend that does not need it is live everywhere. When a shape
has to change, the frontend handles both shapes first, ships, and the backend changes
after. The same goes for requests: the API keeps accepting what the live frontend
sends.

## Rollback

Fastest first:

- **Frontend**: in Vercel, Deployments, open the previous production deployment and
  use **Instant Rollback** (Hobby can roll back to the immediately previous one only).
  After a rollback, Vercel stops assigning new production deployments to the domain:
  once the fix is out, use **Undo Rollback** on the production tile to promote it and
  turn auto-assignment back on.
- **API and worker**: in Railway, each service's Deployments list, open the previous
  successful deployment and **Redeploy** it. Do both services, worker included.
- **Then fix forward**: revert the bad PR on GitHub (or merge a fix), and let it go
  through CI like any change.

When the bad release included a migration: leave the schema where it is. Because
every migration is compatible with the previous release, the redeployed previous code
runs fine against the newer schema, and the worker's pre-deploy treats a database
ahead of the code as ready. Do not run `alembic downgrade` against production unless
the migration itself is the problem, and then only after a backup, from a checkout of
the bad release (only it has the migration to undo), with `DATABASE_URL` exported from
the Postgres service's `DATABASE_PUBLIC_URL` (the internal host is unreachable from
outside Railway): `alembic downgrade -1`. A migration
that fails part-way leaves nothing behind: Postgres runs DDL in a transaction, the
pre-deploy fails, and the deploy stops.

## Kill switch

From fastest to slowest, all in Railway's variables unless said otherwise (a variable
change redeploys the service with the same image, in about a minute):

1. `AUTH_REQUIRED=true` on the api: keyless traffic gets 401 at once. Everyone with a
   key keeps working.
2. Revoke the demo key: `DELETE /admin/keys/{id}` with `X-Admin-Token`. Takes effect on
   the next request, no redeploy.
3. `DAILY_SPEND_CAP_USD=0.01` on both services: every paid call is refused for the rest
   of the UTC day; queued work is held, not failed, until the cap resets. It must stay
   above 0, or the services will not start with `APP_ENV=prod`.
4. Provider-side monthly limits in the Anthropic and Exa consoles: the backstop that
   does not depend on this code being right. Set them before going live.

To stop everything, remove the active deployment of both services (the deployment's
menu, **Remove**); data in Postgres stays.

## Watching it

Logs are in Railway, per service, under the deployment's **Logs** (and across
services in **Observability**). With `LOG_JSON=true` every line is one JSON object
with an `event` field, which the log search can filter on. Events worth knowing:

- `spend_threshold_crossed`: settled spend for the UTC day reached
  `SPEND_ALERT_FRACTION` of the cap. Once a day. Check `GET /admin/usage` for which
  provider and operation, and whether it is the nightly run (`background_usd`) or
  visitors (`interactive_usd`).
- `spend_cap_reached`: a call was refused by the cap. Interactive requests get a
  warning or 503; jobs are held until 00:00 UTC (plus jitter) and resume themselves.
- `llm_model_unpriced`, at startup: `LLM_MODEL` has no entry in `LLM_PRICES_JSON`, so
  every call is costed at the pessimistic fallback rate. Fix the prices.
- `limiter_fallback`: Redis is unreachable and limits are per process until it
  answers (logged at most once a minute). `/health` shows `redis: unreachable`.
- `job_dead`: a job failed all its attempts. `GET /admin/queue` lists the last 20 dead
  jobs with their errors. A ticker that does not exist ends here once, by design.

`GET /admin/usage` and `GET /admin/queue` are the dashboards. Both need the
`X-Admin-Token` header.

## Cost

As of 2026-10-09, from each provider's pricing page:

- **Railway Hobby**: $5 a month, which includes $5 of usage. Usage is metered per
  second at $20 per vCPU-month, $10 per GB of memory a month, $0.15 per GB of volume
  and $0.05 per GB of egress. An idle-most-of-the-day demo of four small services
  (api, worker, Postgres, Redis) needs roughly 0.7 GB of memory and a fraction of a
  vCPU on average. That is an *estimate* of about $8 to $12 of usage a month, so $5 to
  $10 above the plan fee. Check the project's Usage page after the first week.
- **Vercel Hobby**: free, for personal and non-commercial use, with 100 GB of transfer
  a month.
- **Exa and Anthropic**: bounded by `DAILY_SPEND_CAP_USD`. At $3 a day the worst case
  is about $90 a month; a quiet demo spends far less, mostly the nightly pre-warm,
  which may use at most 60% of the cap. The provider-side monthly limits bound it
  whatever the code does.
