# Metrix v2 changelog

One entry per phase, newest first. Each phase ships on its own branch
(`v2/<phase>-<slug>`) and pull request.

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
