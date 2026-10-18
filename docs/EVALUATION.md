# Evaluating relevance scoring

Metrix links a movement to the articles an LLM judges explain it. This document
describes how that judgement is measured, and what has been measured so far.

## Status

**No numbers yet.** The harness, the metrics, replay and the CI gate are built and
tested, but the labelled set is empty: `evals/golden.jsonl` has no `human` or `auto`
labels, `evals/snapshots/` has no snapshots, and there is no `evals/baseline.json`. The
scorer (v1) is still unevaluated. Building the set needs the production database's
enriched movements and a person's time (below); neither was available where this was
written.

## Method

1. **Snapshots** (`python -m evals.snapshot`). For each enriched movement, the
   candidate set the scorer saw is rebuilt from `news_query_cache` (the raw search
   payloads) and frozen into `evals/snapshots/<SYMBOL>_<date>.json`, with the
   movement's decomposition, session legs, peer set, each candidate's publication time
   and session bucket, and the SEC filings around it. Labels refer to snapshot
   candidates, so results reproduce even though live search results drift. A snapshot
   whose searches cannot all be rebuilt says so (`complete: false`, with notes).
2. **Labels** (`scripts/label_movements.py`). One line per movement in
   `evals/golden.jsonl`: `symbol`, `date`, `driver` (one sentence), `tier`, `accepted`
   (candidate ids, URL patterns, or filing accession numbers), `no_clear_driver`, and
   `label_source`:
   - `human`: chosen by a person in the labelling tool;
   - `auto`: mechanical only. One rule: an 8-K with Item 2.02 (earnings) accepted
     overnight, after the prior close and before the open, explains the move
     (`evals/auto_label.py`);
   - `proposed`: drafts for a person to confirm. **Never counted** in headline numbers.
3. **Metrics** (`python -m evals.run`; definitions in `evals/metrics.py`):
   Hit@1, Recall@5, tier accuracy of the top accepted link (with a confusion matrix),
   abstention precision and recall against "no clear driver" labels, timing violations
   (links in the `later` bucket scored at or above `RELEVANCE_MIN_SCORE`; target zero),
   calibration (accepted share per score bucket: 0.35 to 0.6, 0.6 to 0.9, 0.9 to 1.0),
   cost and latency per movement, and 95% bootstrap intervals (1,000 resamples over
   movements, fixed seed) on the headline metrics.
4. **Replay.** Model answers are recorded in `evals/recordings/`, keyed by a hash of
   model, operation, prompts and output schema. `--replay` recomputes everything from
   them with no network or key; a prompt with no recording fails the run rather than
   being skipped. `--live --budget-usd <n>` re-scores through the normal provider seam
   (rate limited, recorded in the spend ledger as `eval_relevance`) and stops before a
   call once the run's ledger spend reaches the budget.
5. **Gate.** CI runs `python -m evals.run --replay --gate`, which fails when Hit@1,
   Recall@5 or tier accuracy drops more than the tolerance in `evals/baseline.json`
   below its baseline, or timing violations rise. It passes, saying why, while there
   are no labels or no baseline.

## Set composition (target)

60 to 80 movements across at least 12 tickers in at least 6 sectors, covering all
three tiers, both directions, earnings and non-earnings days, and some days with no
clear driver. `python -m evals.coverage` reports the current set against these
targets, how many movements are queued for labelling, and (with
`--spend-per-enrichment`) what enriching a shortfall would cost.

## Today's numbers (scorer v1)

None: there is no labelled set yet.

## Failure modes

None measured yet. Known, from the code: the v1 prompt labels timing in calendar days
relative to the move's date in UTC, so an article published at 20:30 New York time the
evening before reads as the same day; and it tells the model that after-close articles
(T+0 or T+1) can explain the move. Scorer v2 uses the session buckets instead, and
never links a candidate published after the next session's open (timing violations
are zero by construction for candidates with a known time).

## Comparing scorer v1 and v2

`--scorer v1|v2` (default `RELEVANCE_SCORER_VERSION`) picks the scorer; both run on the
same snapshots. v2 also sees the snapshot's decomposition, session legs and filings
(as candidates, so a label may accept a filing by its accession number), and may
abstain, which the abstention metrics score against `no_clear_driver` labels.

```bash
python -m evals.run --live --budget-usd 2 --scorer v1 --name scorer-v1
python -m evals.run --live --budget-usd 2 --scorer v2 --name scorer-v2
```

v2 becomes the default only if it beats v1 on Hit@1 and Recall@5 with overlapping or
better intervals and no rise in timing violations. Until then it is opt-in. Neither has
been run on real data yet.

## How to build the set and reproduce

```bash
# 1. Snapshots, read-only, from a database that has the enriched movements (a
#    restored copy of production is safest), with filings from EDGAR:
DATABASE_URL=postgresql+asyncpg://... SEC_USER_AGENT="Metrix research you@example.com" \
  python -m evals.snapshot --symbols NVDA,AAPL,TSLA,MSFT,AMZN --filings

# 2. Composition, and the labelling queue
python -m evals.coverage

# 3. Drafts (auto and proposed), then label for real
python scripts/label_movements.py --propose
python scripts/label_movements.py

# 4. Score the set once for real, with a budget, recording the answers
python -m evals.run --live --budget-usd 2 --name scorer-v1

# 5. From then on, anywhere, with no key
python -m evals.run --replay --name scorer-v1
```

Commit the snapshots, the labels, the recordings and `evals/results/scorer-v1.json`,
then write `evals/baseline.json` from its metrics:
`{"metrics": {"hit_at_1": ..., "recall_at_5": ..., "tier_accuracy": ..., "timing_violations": ...}, "tolerance": 0.05}`.

## Verifying EDGAR's acceptance time zone

`acceptanceDateTime` carries a trailing `Z`, but Metrix reads it as New York wall time
(`SEC_ACCEPTANCE_TIMEZONE`), because an after-close earnings 8-K reads `16:20`. That is
pinned by a test but not yet checked against a live filing. To check: fetch
`https://data.sec.gov/submissions/CIK0001045810.json` with a User-Agent, find the 8-K
with items `2.02` filed on 2024-05-22, and compare its `acceptanceDateTime` with the
time NVIDIA's release crossed the wire (after the 16:00 close). If they agree as New
York time, the setting is right; if the value matches UTC, set
`SEC_ACCEPTANCE_TIMEZONE=UTC`.
