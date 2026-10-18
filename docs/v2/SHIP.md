# Shipping v2: the runbook

The v2 work is a stack of branches, each one built on the previous:

```
main
  fix/chart-below-1024            (the hotfix, on its own)
  v2/0-audit-baseline
  v2/1-finance-core
    v2/2-eval-harness
      v2/3-primary-sources
        v2/4-research-api
          v2/5-ask-metrix
            v2/6-frontend
              v2/7-ship-docs
```

None of it has been deployed. This runbook is the order to do it in. Nothing in it
raises a cap or changes a default that alters stored data.

## 1. Merge
- Open one pull request per branch, each against the branch below it. The PR text is in each phase's checkpoint.
- The hotfix goes to `main` on its own.
- Merge in order, so CI runs on each branch.
- Every deploy is compatible with the previous one ([DEPLOYMENT.md](../DEPLOYMENT.md), "Rules that keep separate deploys compatible"). Every migration only adds.

## 2. Migrations (run by the release command)
- Six migrations, all additive:
  - `c5d8e2a7f9b1` finance core;
  - `d2e6f3b8a1c4` primary sources;
  - `e8b4c1d6f2a7` ticker data version;
  - `f3c9a7d1e5b2` symbol search trigram;
  - `a6d2e9c4b8f1` watchlists;
  - `b7e3f1a9c5d2` article search.
- `f3c9a7d1e5b2` creates `pg_trgm` indexes only where the extension can be created. Without it, search still works, just without that index. Check the release log for a skipped-index notice.
- `b7e3f1a9c5d2` adds a generated column over every article. On a large `news_articles` table this rewrites the table under a lock. Run it outside market hours.

## 3. Settings
- **None is required.** The defaults keep v1's behaviour:
  - `MOVEMENT_DETECTION_MODE=raw`
  - `RELEVANCE_SCORER_VERSION=v1`
  - `SEC_EDGAR_ENABLED=false`
- **Optional:**
  - `METRICS_TOKEN`, to enable `/metrics`;
  - `SEC_EDGAR_ENABLED=true` with `SEC_USER_AGENT="Metrix research <contact>"` (filings are free, at 5 requests a second).
- **The web app needs nothing new on Vercel.** `vercel.json` adds the SPA rewrites that deep links (`/t/NVDA/m/...`) need.
- **CORS.** The API now allows `PUT` and `DELETE` (watchlists) and exposes the rate-limit headers. `CORS_ALLOWED_ORIGINS` is unchanged.

## 4. After the deploy
1. **Smoke test:** **Actions > Smoke > Run workflow**, or `python scripts/smoke.py`.
2. **Backfill the decomposition** (free: prices only):

   ```
   curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" https://<api>/admin/backfill-analytics
   ```

   Then watch `GET /admin/queue` until the `backfill_analytics` jobs are done.
3. **Run the nightly run once**, within the existing cap:

   ```
   curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" https://<api>/admin/prewarm
   ```

   It enriches the most recent session's moves first.
4. **Check that the seed tickers' recent large moves have complete evidence:**

   ```
   python scripts/baseline_remote.py movements --base-url https://<api> --json-out movements.json
   ```

   Moves that say `budget_deferred` are waiting on the daily cap. They are enriched on following nights, or on demand. Raising `DAILY_SPEND_CAP_USD` instead is Eungi's decision.

## 5. Showcase case files
1. **Pick candidates** with `GET /market/pulse?sessions=5`, and, for each seed ticker, `GET /tickers/{symbol}?min_magnitude_pct=5`. You want:
   - one move whose breakdown is mostly company, ideally with an earnings 8-K;
   - one mostly sector;
   - one mostly market (macro).
2. **Open each at `/t/<symbol>/m/<date>`** and check by hand:
   - the parts add up to the move;
   - the filing's time and session bucket match EDGAR;
   - the top article really explains the move;
   - nothing in the "later" bucket is ranked as a cause.
3. **Link the three** in the README under "Live demo". Replace the screenshot with one of them, taken at 1440 px (`npm run screenshots` takes the sample data; for live pages, take it by hand).
4. **Record the walkthrough** ([WALKTHROUGH.md](WALKTHROUGH.md)).

## 6. The numbers still to run
See [RESULTS.md](RESULTS.md), "Pending":
- relevance labels and both scorers' evaluations;
- the model's chat evaluation;
- the cost of `union` on real prices.

Each needs real data, a person or a budgeted run. Each command writes a results file to commit.
