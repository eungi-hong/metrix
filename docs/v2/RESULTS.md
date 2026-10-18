# Metrix v2: results

One-sentence claims you can quote, each with its number and the command that
reproduces it. Every number here is **synthetic-local** unless it says otherwise:
one 2-vCPU machine, synthetic data built by the real pipeline, a fake model with a
fixed delay. These numbers compare v1 and v2 on identical inputs. They say nothing
absolute about production.

The claims that need real data (relevance accuracy, the model's own chat answers,
the cost of union detection) are listed separately, with their commands. They are
not reported until those commands have been run.

## Claims

1. **A warm ticker read went from 490 ms to 16 ms median at 50 simulated users,
   because a read now revalidates against a per-ticker data version instead of
   rebuilding the response.**
   p95 790 to 51 ms, throughput 26.3 to 35.5 reads a second (offered load), no
   errors either way. Locust, 60 s, one uvicorn worker, five hot tickers.
   Commands: [docs/PERFORMANCE.md](../PERFORMANCE.md#reproducing). Raw output:
   `docs/v2/loadtest/before-u50_stats.csv` and `after-u50-reval_stats.csv`.

2. **At 150 simulated users v1's read path collapsed (3.2% errors, 28 s p95) and v2
   served 87 reads a second with no errors and a 360 ms p95.**
   v1 queued on the database pool until its 30 s timeout; v2 answers mostly from a
   `304` or the Redis response cache. Same commands, `-u 150`.

3. **Ask Metrix's citations check out: on 18 fixed questions its database fallback
   scored citation precision 1.000, with 0 uncited numeric claims and 0 numbers that
   disagree with the cited movement.**
   Required recall 0.821; both unanswerable questions answered without citations.
   Deterministic checks, no model grading. This is the no-model path; the model's
   own numbers are pending (below).
   Command, on a seeded bench database:
   `NEWS_PROVIDER=fixture python scripts/bench_read_path.py seed && python -m evals.chat_eval --fallback`.
   Result: `evals/results/chat-fallback.json`.

4. **The web app's first load is lighter and fast: entry bundle 170 kB to 109 kB
   gzipped, and Lighthouse desktop performance 97 to 99 with accessibility 100 on
   the home, ticker and case-file pages.**
   Measured on the production bundle served locally with the mocked API, so it
   excludes API latency.
   Commands, from `frontend/`:
   - `VITE_METRIX_API_BASE_URL=https://api.example.invalid npm run build`, which prints the entry chunk;
   - for Lighthouse, build with `VITE_MSW=1`, run `VITE_METRIX_API_BASE_URL=http://api.test npx vite preview`, then `npx lighthouse http://127.0.0.1:4173/t/NOVA --preset=desktop`.

5. **The test suite grew from 575 to 879 backend tests (with Postgres and Redis),
   plus 34 unit and 19 browser tests for the web app, including axe and 390 px
   layouts.**
   Commands:
   - `TEST_DATABASE_URL=... TEST_REDIS_URL=... pytest`
   - `cd frontend && npm test && npm run e2e`

Also measured, smaller:
- **Symbol search.** At 20,000 directory rows, the search query went from a 12.7 ms sequential scan to 0.34 ms over a pattern index and trigram indexes. This came from `EXPLAIN ANALYZE` on local Postgres 16, and there is no script.
- **Ask Metrix at 50 users.** No fallbacks, and about 0.1 s of Metrix's own time per question on top of the model's.

## Numbers that did not improve

- **Ask Metrix answers take longer.** With the same 1.0 s fake model at 50 users, the median went from 1.1 s (Phase 4, one model call) to 2.1 s (Phase 5). A tool-using answer makes at least two model calls: a lookup, then the answer. Phase 4 traded that latency for answers grounded in lookups rather than a fixed context. Source: [docs/PERFORMANCE.md](../PERFORMANCE.md), `docs/v2/loadtest/p5-u50-reval_stats.csv`.
- **The provider's rate limit still caps questions at a spike.** With the default `OPENAI_MAX_RPM=50`, a 50-user run of the v1 code answered 56 of 105 questions (53%) with 503 (`docs/v2/loadtest/before-u50-default-rpm_stats.csv`). v2 now answers those with the cited database summary instead of an error, but it is still not a model-written answer. Each question now takes two or more calls, so the ceiling is about 25 questions a minute per OpenAI account. The fix is a higher account tier, not code.
- **Lighthouse best practices is 81.** The only failing audit is HTTPS, because the local preview is plain HTTP. It should pass on Vercel, but that is unmeasured.

## Pending: needs real data, a person, or spend

| Claim it would support | Command | Needs |
|---|---|---|
| Top-1 explanation accuracy (Hit@1), Recall@5, tier accuracy and calibration of scorer v1, with 95% intervals | `python -m evals.snapshot --symbols NVDA,AAPL,TSLA,MSFT,AMZN --filings`, `python scripts/label_movements.py`, then `python -m evals.run --live --budget-usd 2 --scorer v1 --name scorer-v1` | the production database, 60 to 80 labelled moves, about $2 |
| Scorer v2 against v1 on the same labels | `python -m evals.run --live --budget-usd 2 --scorer v2 --name scorer-v2` | the same, about $2 |
| The model's chat numbers (precision, recall, uncited claims, latency) | `python -m evals.chat_eval --live --budget-usd 1 --name chat-v2` on a seeded bench database | an OpenAI key, under $1 |
| How many more moves `union` flags than `raw`, and what enriching them would cost a day | `python scripts/measure_detection.py --symbols NVDA,AAPL,TSLA,MSFT,AMZN --usage-json usage.json --json-out detection_production5.json` | yfinance and the production spend ledger |
| The README's detection table on real prices | `python scripts/measure_detection.py --symbols NVDA,KO,JNJ --json-out detection_readme.json` | yfinance |
| Production read latency and spend per enrichment | `python scripts/baseline_remote.py movements` and `usage` ([BASELINE.md](BASELINE.md), section 3) | the deployment |

## Daily cost impact of v2 (by construction, not measured)

- **Unchanged by default.**
  - Detection stays `raw`, so the nightly run enriches the same moves.
  - Scorer v2 is off.
  - SEC filings are off until `SEC_EDGAR_ENABLED=true`, and are free when on.
  - The decomposition backfill and the peers' prices are free (yfinance only).
- **Ask Metrix costs more per question, within a hard bound.**
  - A question makes 2 to 5 model calls instead of 1.
  - Its tokens are capped at `CHAT_TURN_TOKEN_BUDGET` (40,000) and 1,200 output tokens per call.
  - At the production gpt-4o-mini prices in `.env.production.example`, the worst case is about $0.01 a question.
  - Questions draw on the same daily cap (`DAILY_SPEND_CAP_USD=3` in production) as everything else.
  - Over the cap, the answer becomes the free database summary, and the quota is given back.
- **Caching lowers database load, not spend.** No model or search call was ever made on a read.
