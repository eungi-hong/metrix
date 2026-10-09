# Task: Put Metrix live, on a setup that keeps up with ongoing changes

You are working in the `metrix` repository. The goal is a public link a reviewer can open:
the React frontend on **Vercel**, and the backend (API, worker, Postgres, Redis) on
**Railway**. If Railway turns out to be unworkable for a concrete reason you find, stop and
tell me before switching to Render or Fly.io.

**Both the backend and the frontend are still being changed, in parallel, by other
sessions.** That is the main constraint. You are setting up deployment, not finishing
features, so:

- Do **not** change application behaviour (movement detection, ingestion, scoring, chat,
  quotas, the UI). The only code changes allowed are the small production-readiness fixes
  listed in Stage 1. If you think something else needs changing, stop and ask.
- Never stage, commit, revert, reformat or "tidy" files you did not create or that Stage 1
  does not name. The working tree may contain other people's uncommitted work. Use
  `git add <path>` with explicit paths, never `git add -A` or `git add .`.
- Do all your work on a branch called `deploy/live`, not on `main`.
- The goal is a pipeline where future changes go live just by merging to `main`, with
  checks in front of them. A one-off manual deploy of today's code is not enough.

Before changing anything, read `README.md`, `docs/FAIRNESS.md`, `docs/PREWARMING.md`,
`Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml`, `.env.example`,
`app/core/config.py`, `app/main.py`, `app/api/deps.py`, `app/services/auth.py`,
`app/worker.py`, `app/api/routes/health.py`, `app/api/routes/admin.py`,
`scripts/create_api_key.py`, `frontend/package.json`, `frontend/vite.config.ts`,
`frontend/.env.example` and `frontend/src/App.tsx`. Match the existing style: module
docstrings that explain *why*, type hints, structlog events, every tunable in
`app/core/config.py` with a `Field(description=...)`, and tests that need no network,
Docker or keys.

**Work in stages. At the end of each stage, stop, report what you did and what you found,
and wait for me to say "continue".** Several steps change hosted infrastructure or spend
money, so check in each time.

## Facts about the code you must account for

- `APP_ENV=prod` refuses to start without `DAILY_SPEND_CAP_USD`. The cap must be `> 0`.
- `LLM_PRICES_JSON` ships commented out. If it is missing, LLM calls are costed at the
  pessimistic fallback rates; if it were set to zeros, the cap would never trip. Production
  needs real prices. **Ask me for them. Never guess a price.**
- `AUTH_REQUIRED` defaults to true. When false, keyless callers are served on the
  `anonymous` plan, keyed by client IP (`app/services/auth.py::client_ip`).
- `client_ip` reads `X-Forwarded-For` only when `TRUSTED_PROXY_COUNT > 0`. Behind
  Railway's proxy, a value of 0 means every anonymous visitor shares the proxy's IP and
  therefore one quota bucket. A value that is too high lets clients spoof their IP. The
  correct number has to be **measured** on Railway, not assumed.
- The `database_url` validator upgrades `postgresql://` to `postgresql+asyncpg://`, but not
  `postgres://`, which some hosts hand out.
- The Dockerfile hard-codes `--port 8000`. Railway injects `$PORT`.
- `docker-entrypoint.sh` runs `alembic upgrade head` in every container unless
  `SKIP_MIGRATIONS` is set. The worker sets it in compose.
- Every worker process runs the nightly scheduler, deduplicated in the database. Rate
  limits (`EXA_MAX_RPS`, `ANTHROPIC_MAX_RPM`) are **per process**.
- CORS is an exact-origin allow-list (`CORS_ALLOWED_ORIGINS`) with methods GET, POST and
  OPTIONS only.
- The frontend reads `VITE_METRIX_API_BASE_URL` **at build time** and defaults to
  `http://localhost:8000`. It stores a user's API key in `sessionStorage`. It has a
  "View demo data" mode backed by local fixtures.
- The symbol directory (`SYMBOL_DIRECTORY_MODE=enforce`) is filled by a weekly refresh job.
  Until it has rows, enforce acts as warn.
- There is no CI (`.github/` does not exist).

## Stage 0: Audit. Change nothing.

1. Run `git status` and `git log --oneline -10`, and list the uncommitted and untracked
   files. If there is in-progress work, **do not touch it**. Tell me which of it looks
   unfinished (for example, a migration with no model, or a component not wired into
   `App.tsx`), because whatever is on `main` when the pipeline goes live is what deploys.
2. Run the backend tests (`pytest`) and the frontend checks (`npm ci`, then `npm run
   lint`, `npm run typecheck`, `npm test` and `npm run build` in `frontend/`). Report
   anything that fails. Don't fix failures in application code; tell me.
3. Check that `alembic upgrade head` from an empty database reaches a single head
   (`alembic heads`). Two heads means parallel work produced diverging migrations, and
   that blocks deployment. Report it if so.
4. Check which CLIs are installed and logged in: `gh`, `railway`, `vercel`. Don't install
   or log in to anything yet.
5. Report back with: the state of the tree, the test results, the migration heads, the
   CLIs available, and anything in the list above that turned out to be wrong.

## Stage 1: Small production-readiness fixes (on `deploy/live`)

Each fix gets a test where it is testable. Keep the diffs small.

1. **Port.** Make the API listen on `$PORT` when it is set and 8000 otherwise, without
   breaking `docker compose up`. The entrypoint must still `exec` so signals reach
   uvicorn, and the worker's graceful shutdown on SIGTERM must keep working.
2. **Database URL.** Also accept `postgres://` and upgrade it to `postgresql+asyncpg://`.
   Add a test.
3. **Migrations run once.** In production, migrations should run once per deploy as a
   pre-deploy step, not in every container. Keep the entrypoint behaviour for compose.
   Document that both Railway services set `SKIP_MIGRATIONS=1`, and that the API service's
   pre-deploy command is `alembic upgrade head`.
4. **CORS for preview deployments.** Vercel preview URLs change on every push. Add an
   optional `CORS_ALLOWED_ORIGIN_REGEX` setting, passed to `CORSMiddleware`'s
   `allow_origin_regex`, empty by default. Document that it must be anchored and specific
   to this project, for example `^https://metrix-[a-z0-9-]+-eungi-hong\.vercel\.app$`,
   never a broad `.*\.vercel\.app`. Add a test that a matching origin gets CORS headers and
   a non-matching one does not.
5. **Frontend build guard.** A production build with no `VITE_METRIX_API_BASE_URL` would
   silently point at `localhost:8000`. Make `vite build` fail with a clear message when
   `mode === "production"` and the variable is unset. Dev and tests keep the localhost
   default.
6. **SPA routing.** If the frontend uses client-side routes, add a `frontend/vercel.json`
   with a rewrite to `index.html`. If it doesn't, say so and skip this.
7. **Proxy-header diagnostics.** Add a guarded way to see what the API receives as the
   socket peer and `X-Forwarded-For`, so Stage 4 can measure `TRUSTED_PROXY_COUNT`. An
   admin-token-protected endpoint or a one-off structlog line is fine. It must never be
   reachable without the admin token. Plan to remove it in Stage 4 if it is temporary.

Commit each fix separately on `deploy/live`, with clear messages. Stop and report.

## Stage 2: Pipeline files, CI and the runbook

1. **CI** at `.github/workflows/ci.yml`, on pull requests and on pushes to `main`:
   - backend: Python 3.12, `pip install -r requirements.txt -r requirements-dev.txt`,
     `ruff check .`, `pytest` (offline, as the suite already is). If the suite has
     Postgres/Redis-backed tests gated on `TEST_DATABASE_URL`/`TEST_REDIS_URL`, run those
     too with service containers;
   - a migration check: `alembic upgrade head` against a Postgres service container,
     failing on more than one head;
   - frontend: Node LTS, `npm ci`, `lint`, `typecheck`, `test`, `build` (with a dummy
     `VITE_METRIX_API_BASE_URL`);
   - Docker: `docker build .`, so a broken image fails before Railway sees it.
2. **Railway config as code.** Two services from the same repo and Dockerfile:
   - `api`: the default CMD, `SKIP_MIGRATIONS=1`, pre-deploy `alembic upgrade head`,
     health check on `/health`, 1 replica;
   - `worker`: `python -m app.worker`, `SKIP_MIGRATIONS=1`, no public domain, 1 replica,
     and a draining window longer than `WORKER_SHUTDOWN_TIMEOUT_SECONDS`.
   Use per-service config files (for example `railway.api.json` and
   `railway.worker.json`) if Railway's config-as-code needs that. Check the current
   Railway docs for the exact schema; don't write it from memory. Use the Railway Postgres
   and Redis plugins and reference their variables.
   Set auto-deploy to `main` with **"wait for CI"** turned on, so a red build never ships.
3. **Vercel**: root directory `frontend`, framework Vite, production branch `main`.
   Production gets `VITE_METRIX_API_BASE_URL` set to the Railway API URL. Preview
   deployments use the same API URL, allowed by the CORS regex.
4. **`.env.production.example`** at the repo root: every variable production needs, with
   a one-line comment each, no real values, and the recommended values below:
   - `APP_ENV=prod`, `LOG_LEVEL=info`;
   - `DAILY_SPEND_CAP_USD=3`, `BACKGROUND_SPEND_SHARE=0.6`;
   - `LLM_PRICES_JSON=` (to be filled from the provider's pricing page) and
     `EXA_COST_ESTIMATE_USD`;
   - `AUTH_REQUIRED=false`, with an anonymous plan in `PLAN_LIMITS_JSON` that can browse
     pre-warmed tickers and chat a little but can't trigger cold ingests:
     `{"anonymous": {"cold_ingests_per_day": 0, "refresh_per_day": 0, "chat_per_day": 5,
     "allow_wait": false}}`. Check the field names against `PlanLimits`. The idea: a
     reviewer who opens the link with no key sees real pre-warmed data, and anything that
     costs real money needs the demo key;
   - `TRUSTED_PROXY_COUNT=` (measured in Stage 4);
   - `CORS_ALLOWED_ORIGINS=` (the Vercel production domain) and `CORS_ALLOWED_ORIGIN_REGEX=`;
   - `ADMIN_TOKEN=` (long and random, generated by me);
   - `PREWARM_SEED_SYMBOLS` set to a small set (NVDA, AAPL, TSLA, MSFT, AMZN), and
     `PREWARM_TOP_N` and `PREWARM_MAX_ENRICHMENTS_PER_RUN` kept small so the nightly run
     fits inside the background share of the cap;
   - `WORKER_CONCURRENCY`, `WORKER_INTERACTIVE_SLOTS`, and rate limits sized for one worker
     process.
5. **`docs/DEPLOYMENT.md`**, the runbook, covering:
   - the architecture (Vercel → Railway API → Postgres/Redis, plus the worker) and what
     each platform setting is and why;
   - **how ongoing changes ship:** feature branch → PR → CI → Vercel preview → merge to
     `main` → Railway and Vercel deploy automatically;
   - **migration rules while two services deploy separately:** migrations run before the
     new code starts, so every migration must work with the *previous* release still
     running. Additive changes only (new nullable columns, new tables). Renames and drops
     are split into expand, deploy, then contract in a later release;
   - **API/frontend compatibility:** the frontend may deploy before or after the API.
     Backend changes to response shapes must be additive, or the frontend must handle both
     shapes until both are out;
   - **rollback:** redeploy the previous Railway deployment and promote the previous Vercel
     deployment, plus what to do when the bad release included a migration;
   - **the kill switch:** fastest first: set `AUTH_REQUIRED=true` (keyless traffic stops),
     revoke the demo key (`DELETE /admin/keys/{id}`), lower `DAILY_SPEND_CAP_USD` to
     something like `0.01` (it must stay `> 0`), and the provider-side monthly limits;
   - where the logs are, and what `spend_threshold_crossed`, `limiter_fallback` and dead
     jobs mean when they show up;
   - the cost: Railway's monthly cost for two small services plus Postgres and Redis,
     Vercel Hobby, and the spend cap. Check current pricing; don't guess.
6. Add a short "Live demo" section to `README.md` with a placeholder link, how to get a
   key, and a pointer to `docs/DEPLOYMENT.md`. **Do not touch `SUBMISSION.md`.**

Open a PR from `deploy/live` to `main` with `gh` if it is available, and let CI run.
**Don't merge it.** Stop and report.

## Stage 3: Provisioning, with me

You can't click dashboards, and I will enter every secret myself. So:

1. Give me an exact numbered checklist for Railway and Vercel: what to create, which repo
   and branch, which root directory, which settings to turn on (including "wait for CI").
   If the `railway`/`vercel` CLIs are installed and logged in, you may run non-secret
   steps with them, after telling me each command first.
2. Give me the list of variables to set on each service, from `.env.production.example`.
   **Never ask me to paste a secret into this conversation. Never write a secret to a
   file, a commit, a command line or a log.** For generated secrets like `ADMIN_TOKEN`,
   give me a command I run myself (for example `openssl rand -hex 32`).
3. Remind me to set monthly spend limits in the Anthropic and Exa consoles, as a backstop
   that doesn't depend on our code.
4. Wait until I tell you both deploys are green, then stop.

## Stage 4: Bootstrap and verification against the live URLs

Use the admin token from an environment variable that I export in your shell
(`METRIX_ADMIN_TOKEN`). Never print it.

1. `GET /health` should report `status: ok`, database ok, Redis ok, and the news and LLM
   providers configured.
2. **Measure `TRUSTED_PROXY_COUNT`** with the Stage 1 diagnostic: make a request, look at
   the peer and the `X-Forwarded-For` hops, and work out how many entries Railway's
   proxies add. Tell me the value and why, I'll set it, then confirm two different clients
   resolve to different IPs and a forged leading `X-Forwarded-For` entry is ignored.
   Remove the diagnostic if it was temporary (that goes through a PR like everything else).
3. Enqueue the symbol-directory refresh and confirm the directory fills up, so `enforce`
   actually rejects a fake ticker with a 404.
4. Create a demo user on the `free` plan and one API key through the admin endpoints.
   Hand me the key **once**, in your final message for this stage only, and never write it
   to a file.
5. Pre-warm the seed symbols (`POST /admin/prewarm`), follow `GET /admin/queue` until it
   drains, then check `GET /tickers/NVDA` returns movements with linked news.
6. **Prove the spend cap is live:** `GET /admin/usage` for today must show non-zero cost
   for the Exa and LLM calls the pre-warm made, and no `cost_estimated=true` entries for
   the production model. If anything is costed at $0 or at the fallback rate, the cap is
   not doing its job. Stop and tell me.
7. Write `scripts/smoke.py` (httpx, reading `METRIX_URL` and an optional key from env)
   that checks: `/health`, a keyless `GET /tickers/NVDA` (200), a keyless request for an
   un-warmed ticker refused by the anonymous plan's cold-ingest limit, a fake ticker (404),
   a chat with the demo key, and CORS headers for the Vercel origin. It must make at most
   one paid call (the chat). Add a manually triggered GitHub Actions workflow that runs it
   against production, with the URL and key stored as repository secrets.
8. Open the Vercel URL in a browser if you can (otherwise give me a checklist): keyless
   browsing of NVDA works, entering the demo key works, a cold ticker shows job progress,
   chat answers, and the console shows no CORS or mixed-content errors.

Stop and report: the live URLs, the smoke results, today's spend so far, and anything
that surprised you.

## Stage 5: Hand-off

1. Fill in the real link in the README's "Live demo" section, through a PR.
2. Give me a short summary: what's deployed where, how a change I merge tomorrow reaches
   production, the three-line kill switch, and the monthly cost.
3. List anything you deliberately left out or would do next (for example a staging
   environment, uptime monitoring, or alerting on `spend_threshold_crossed`), and don't
   build those.
