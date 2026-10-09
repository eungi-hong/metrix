# Pre-warming

Pre-warming computes the data users are likely to ask for before they ask, so that
popular tickers are already warm. This document grows with the work. The fixes below
came first, because both undermined pre-warming: one made the cheapest shared search
unshared, the other made early results permanent.

## Fixes that came first

### The Hard-tier cache was not shared across tickers

The Hard-tier search is meant to be company-free, so that every ticker in a sector on
a given date produces the same request and they all share one `news_query_cache` row.
The query text was company-free. The request was not. `build_tier_queries` attached
one `summary_query` to all three tiers, and that summary named the company and the
size of its move ("Does this article explain why NVIDIA stock moved -4.10% on
2026-06-05?"). `NewsSearchRequest.cache_fingerprint` hashes every field of the request,
so the Hard-tier key differed per ticker and the sharing described in the code, the
README and the submission write-up never happened. Each ticker paid for its own macro
search.

The Hard tier now has its own summary question, which depends only on sector and date:
"Which macroeconomic, political or regulatory developments in this article would move
{sector} stocks around {date}?" Easy and Medium keep the company-specific summary. A
test ingests two differently named companies in the same sector and asserts exactly one
upstream Hard-tier call.

This has a cost. Exa writes each result's summary against the summary question, and
the scorer reads that summary. For macro articles it now reads a sector-level summary
instead of one written for this company's move. The scorer is still told the company,
the size and the direction of the move in its own prompt, and deciding whether a macro
event explains this particular move was always its job, so the loss is a less targeted
snippet, not lost information. The gain is one macro search per sector per day instead
of one per ticker, which is the property the nightly run depends on.

Two workers can now miss on the same cache key at the same moment and both insert it.
The insert runs inside a savepoint, and a unique-constraint violation is logged and
treated as a cache hit, so the losing worker's transaction survives.

### Recent movements were marked complete before their news window closed

A movement's news window runs from three days before the move to one day after it.
Enrichment set `news_status = complete` as soon as it ran, and re-ingestion skips
complete movements. A move enriched on the evening it happened therefore never picked
up the next day's articles, which are often the ones that explain it. The response
cache had the same blind spot: it used a flat 24-hour TTL on the grounds that a past
window's answer never changes, which is only true once the window has closed.

Each movement now stores `news_window_closes_at`, the window's last instant plus
`NEWS_WINDOW_GRACE_HOURS` (default 6) for indexing lag. Enrichment that runs before
then marks the movement `partial`; enrichment after it marks it `complete`. A partial
movement is not re-enriched while its window is still open, and is re-enriched as soon
as it has closed. Today that happens on the next ingestion of the ticker. Once the job
queue exists, the partial pass will enqueue its own follow-up to run at
`news_window_closes_at`.

Re-enrichment re-scores the whole candidate set and replaces the previous verdict.
Articles and links are upserted, so nothing is duplicated, and a link the new pass no
longer supports is removed rather than left with its old score. If the second search
comes back empty, the earlier links are kept, since finding no evidence is not evidence
against them.

The cache TTL now follows the same rule. A response for a closed window is kept for
`NEWS_CACHE_CLOSED_WINDOW_TTL_DAYS` (90). A response for an open window is kept for
`NEWS_CACHE_OPEN_WINDOW_TTL_HOURS` (2), and never past the moment the window closes, so
the first search after closing gets the final answer.

Each enrichment attempt also increments `news_attempts`. A movement that has failed
`NEWS_MAX_ATTEMPTS` times (default 3) is no longer retried automatically; `refresh=true`
still retries it. Without this, a movement whose news can never be fetched would cost a
search and a scoring call on every run.

The migration backfills `news_window_closes_at` and moves rows the old code got wrong,
complete movements whose `news_fetched_at` is earlier than their window close, back to
`partial`, so they are picked up again. `news_status` is a VARCHAR with no CHECK
constraint, so the new value needed no DDL.
