# Metrix v2 changelog

One entry per phase, newest first. Each phase ships on its own branch
(`v2/<phase>-<slug>`) and pull request.

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
  `tests/test_baseline_remote.py`), offline: 575 to 598 backend tests.
