# Metrix v2 changelog

One entry per phase, newest first. Each phase ships on its own branch
(`v2/<phase>-<slug>`) and pull request.

## Phase 7: ship, measure, document (`v2/7-ship-docs`)

- README top rewritten: what Metrix does in 30 seconds, what is new in v2 with a
  number each, a Mermaid architecture diagram, five design decisions, honest
  limitations, every v2 endpoint; architecture tree and tests updated.
- [RESULTS.md](RESULTS.md): quotable claims with their commands, the numbers that
  did not improve, the ones that still need real data, and the daily cost impact.
- [WALKTHROUGH.md](WALKTHROUGH.md): a 90-second recording script.
- [SHIP.md](SHIP.md): merge order, migrations, settings, the backfill and the first
  nightly run, and how to pick and verify the showcase case files. Not deployed
  from here.

## Phase 6: the web app, rebuilt (`v2/6-frontend`)

- Every view has a URL: home, `/t/:symbol` (chart, moves table, selected move),
  `/t/:symbol/m/:date` (the case file), `/ask`, `/pulse`, `/watchlist`,
  `/methodology`. Lazy routes; the table's filters live in the query string.
- A design system from `scratchpad/design/RESEARCH.md`: CSS-variable tokens for
  light and dark, Inter, tabular numbers, one driver palette (market, sector,
  company) used everywhere; a command palette (`/`, `Ctrl+K`), keyboard
  shortcuts (`?`), a phone tab bar.
- The case file: return decomposition bars, "when the news broke" on the
  session timeline, the primary filing, evidence strongest first, why it was
  flagged, peers and benchmarks that session.
- Ask Metrix streams (`POST /chat/stream`), shows which tools it used, and turns
  `[M12]`-style labels into links to the case files they cite.
- Live research progress over `GET /events`; every evidence status worded for
  people; errors and limits explained in plain words (never a quota's internal
  name), with the request id under "Details".
- Charts: lightweight-charts 5.2.1 (Apache-2.0, attribution kept), markers coloured
  by driver, SPY and sector overlays rebased to 100.
- Backend, for the app: `X-RateLimit-Scope` on 429s (and CORS exposes the
  rate-limit headers); ingestion progress has a "detected" stage with session and
  movement counts; the case file carries its session frame; `ApiModel` makes
  defaulted response fields required in the OpenAPI document, so generated types
  are exact.
- Tests: 34 Vitest unit tests; 19 Playwright end-to-end tests (flows, no
  overflow at 390 px on seven pages, axe in dark mode) in CI. Lighthouse
  (desktop, production bundle, mocked API): performance 97 to 99, accessibility
  100.
- Screenshots at 1440, 768 and 390 px in `docs/assets/after/`, from fictional
  sample data (`npm run screenshots`).

## Phase 5: Ask Metrix 2.0 (`v2/5-ask-metrix`)

- A bounded tool loop: six read-only database tools (movements, the case file,
  full-text evidence search, filings, comparison against SPY, watchlist digests), at
  most 5 calls and a token budget per question, the spend cap checked before every
  model call; the stable system prompt and tools first for provider prompt caching.
  Multi-ticker questions; indexed ticker lookup by symbol or company name.
- `tool_turn` on the LLM seam for OpenAI and Anthropic (streamed; Anthropic's prefix
  marked for caching).
- Citations checked against what the tools returned; only cited sources returned;
  `grounded` means cited; `citations_removed`.
- A deterministic database answer when the model is unavailable or capped (200,
  `answered_by: "fallback"`, quota given back) instead of 502 and 503.
- Template follow-up suggestions; history by token budget.
- `POST /chat/stream` (server-sent events).
- `evals/chat_eval.py` and 18 questions; the fallback's numbers recorded.
- Migration `b7e3f1a9c5d2` (Postgres full-text search over articles).

## Phase 4: research API, read path, live updates (`v2/4-research-api`)

- Read path: `tickers.data_version`, bumped at commit by a session hook for every
  write that changes `GET /tickers/{symbol}`; strong ETag and `304`; a Redis
  response cache keyed by the tag; `Cache-Control` for settled data. Load test in
  `loadtest/` and [docs/PERFORMANCE.md](../PERFORMANCE.md): warm reads 490 to 16 ms
  median at 50 users, and no collapse at 150 (synthetic-local).
- `GET /events` (server-sent events from Postgres `LISTEN` on `job_events` and the
  new `ticker_events`, with a polling fallback); the web app drops its 3.5 s poll.
- Research endpoints: symbol search (pattern and trigram indexes on Postgres), the
  movement case file (peers' and benchmarks' same-session returns, detection
  audit), filings, earnings from 8-K Item 2.02, market pulse, watchlists with a
  digest.
- `evidence` status on every movement (why there is none yet, and when).
- Nightly: seed tickers' moves on the run's own session are enriched first; the
  universe's peers' prices ride in the first chunk.
- Operations: `X-Request-ID`, `/health/live`, `/health/ready`, `/metrics`
  (Prometheus, behind `METRICS_TOKEN`).
- Typed contract: `frontend/openapi.json` and generated `schema.d.ts`, checked in CI.
- Migrations `e8b4c1d6f2a7`, `f3c9a7d1e5b2` (Postgres-only, optional indexes),
  `a6d2e9c4b8f1`.

## Phase 3: primary sources and scorer v2 (`v2/3-primary-sources`)

Additive and opt-in: no default changes what is stored or spent.

- Filings stored once (`filings`, unique on accession) and attached to movements in
  their news window with session timing (`movement_filings`); `tickers.cik`.
  `app/services/filings.py`, best effort, behind `SEC_EDGAR_ENABLED`.
- Scorer v2 (`app/services/relevance_v2.py`, `RELEVANCE_SCORER_VERSION=v2`): the
  decomposition, session legs, session buckets, filings as candidates, source quality;
  a `role` per link and movement-level abstention. Links after the next open are never
  kept. Links and filing verdicts record `scorer_version`; stale filing verdicts are
  superseded like links.
- API: `filings`, `evidence_abstained`, `abstain_reason` on movements; `role` and
  `scorer_version` on links.
- `evals.run --scorer v1|v2`.
- Migration `d2e6f3b8a1c4`.
- Deferred (P1): the earnings calendar (3b) and press-release text extraction (3c).

## Phase 2: evaluation harness (`v2/2-eval-harness`)

- `app/services/edgar.py`: SEC EDGAR ticker-to-CIK and filings, fair access (User-Agent,
  rate bucket, SEC hosts only, cache), 8-K items as events. Tested on synthetic
  fixtures; not yet wired into ingestion.
- `evals/`: snapshots rebuilt from the news cache, labels (`human`, `auto`,
  `proposed`), metrics with bootstrap intervals, recorded replay, a budgeted live mode
  through the spend ledger, a CI gate, and a coverage report.
- `scripts/label_movements.py`: the terminal labelling tool.
- `docs/EVALUATION.md`. No labels or numbers yet: the set needs production data and a
  person.

## Phase 1: finance core (`v2/1-finance-core`)

Additive; no default changes what is stored or spent.

- Session timing (`app/services/sessions.py`, `exchange_calendars` XNYS): every new
  link gets `timing` (before_prior_close, overnight, intraday, after_close, later) and
  `timing_precision`; an opt-in news window in sessions (`NEWS_WINDOW_MODE`).
- Return decomposition (`app/services/decomposition.py`): market model with an
  orthogonalised sector factor, Patell SAR, winsorised estimation window; benchmark map
  in config, provisional until checked against real yfinance strings.
- Detection modes (`app/services/detection.py`): `raw` (default, the v1 detector),
  `abnormal`, `union`, with `trigger` on every movement.
- Paid work is never deleted: movements that stop qualifying are kept with
  `qualifies=false` when they have links or a finished enrichment; links a re-score
  drops are marked `superseded_at`. Both hidden from the API and chat.
- Per-movement analytics (`app/services/analytics.py`): gap and intraday legs,
  relative volume, post-move drift, volatility regime.
- Nightly benchmarks in the first price batch; `backfill_analytics` job and
  `POST /admin/backfill-analytics`.
- API: nested `decomposition`, `legs`, `volume_analysis`, `drift`, `regime`;
  `timing` on links; filters `trigger`, `dominant`, `min_sar`; `sort`;
  `include_benchmarks`; `include_unqualified`.
- `scripts/measure_detection.py`: counts per mode on real prices and union's cost.
- Migration `c5d8e2a7f9b1`. Backend tests 604 to 700 (699 pass with Postgres and Redis; 1 skipped until real yfinance profiles are added).

## Phase 0: audit and baseline (`v2/0-audit-baseline`)

No application code changed. Added the tooling and the record that later phases
are measured against:

- `docs/v2/BASELINE.md`: the "before" numbers, each with its command and a label
  (checks, synthetic-local, or production pending).
- `scripts/bench_read_path.py`: seeds a throwaway database with synthetic data
  through the real ingestion pipeline, times `GET /tickers/{symbol}` warm, and
  summarises repeated runs (`seed`, `run`, `report`). Results in
  `docs/v2/baseline/read_path_synthetic_local_run1.json` to `_run5.json`.
- `scripts/baseline_remote.py`: read-only measurements only a deployment can give:
  movements per seed ticker, spend per enrichment from the spend ledger, and chat
  latency and citation behaviour for three fixed questions.
- `scripts/capture_before_screenshots.py` and 18 screenshots in
  `docs/assets/before/` (six states at 1440, 768 and 390 px, from the frontend's
  demo-fixture mode).
- Tests for both measurement scripts (`tests/test_bench_read_path.py`,
  `tests/test_baseline_remote.py`), offline: 575 to 604 backend tests.
