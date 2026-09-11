# Submission 
## 1. Process, assumptions, and key decisions

### Order of work



I built it in the order that lets each stage be verified before the next depends on it:
data model → movement detection (with tests) → news layer → ingestion → API → chat →
Docker and docs. The detector came before anything that consumes it because it is the
only pure logic in the system and the only part I could prove correct in isolation.

### Assumptions

- **Daily bars are enough.** The brief says "major price movements", which I read as
  day-granularity. Intraday data would let you separate an overnight gap from an
  intraday slide, which matters for attributing news, but it is a different data source
  and a much larger ingestion problem.
- **Adjusted close, not close.** Splits and dividends would otherwise manufacture
  movements. NVDA's splits alone would produce several fake "major movements" a year.
- **A rolling year of history by default.** Enough to establish a volatility regime and
  see a useful number of movements, small enough to ingest in one request.
- **The news window is T-3 to T+1.** The news that moves a stock frequently breaks
  before the session it moves in (an overnight filing, a weekend report), and same-day
  follow-ups are often the clearest write-up of the cause.

### Decisions and tradeoffs, by stage

**Movement definition.** `|return| >= max(2%, k × rolling_std)`. The two terms fail in
opposite directions and cover for each other: a flat 2% flags 95 of 251 NVDA trading
days (38% of the year — not a useful definition of "major"), while pure 2σ would flag
noise on a very quiet stock. I made the rolling window exclude the day being tested.
This was the single most consequential detail: including it lets a large move inflate
its own threshold and mask itself, which biases the system against exactly the days it
exists to find. Every parameter is configurable and stored on each movement row, so a
result stays explainable after the config changes underneath it.

**Storage.** Postgres as the system of record, not a cache. Prices are `NUMERIC` because
they are money; returns are computed in float only inside the detector. JSON columns are
declared portably (`JSONB` on Postgres, `JSON` elsewhere) so the endpoint tests can run
on in-memory SQLite — that one decision is why `pytest` needs no Docker, no network and
no API keys.

**News source.** Exa, because the Medium and Hard tiers need articles that explain a
move *without naming the company*. A Fed decision article never mentions NVIDIA, so
keyword search over the ticker structurally cannot find it; neural search can, and Exa
supports the published-date range that pins an article to a movement. Tradeoff: Exa
returns results in relevance order but exposes no numeric score, so ordering is the only
signal it gives — which pushed all the actual judgement into the scoring step, where it
belongs anyway.

**Competitors via LLM, cached, not a static map.** A hand-maintained ticker→competitor
table is accurate the day it is written, stale in a quarter, and only covers tickers
someone thought to add. Since the app accepts any symbol yfinance knows, a static map
would silently reduce the Medium tier to nothing for most inputs. The LLM call is
grounded in yfinance's `sector`/`industry` so it classifies rather than free-associates,
and the result is cached on the ticker row for 30 days — one call per ticker per month.

**One scoring call per movement, not per article or per tier.** The model sees every
candidate side by side, which lets it rank them against each other and reassign the
tier. That last part matters more than I expected: a macro-flavoured search regularly
surfaces a company-specific story, so the link stores both `search_tier` (which query
found it) and `relevance_tier` (what the model says it actually is).

**Cost control** was a design constraint, not an afterthought. Naively, a ticker with 24
movements is 72 searches and hundreds of LLM calls. What bounds it: raw provider
responses cached in Postgres (a past date window's answer never changes); one LLM call
per movement; a configurable cap on movements enriched per run, largest first; cached
peers; and a deliberately company-free macro query so every ticker in a sector shares
one cached search.

**Concurrency.** Two simultaneous requests for a cold ticker must produce one ingestion.
I used a single atomic conditional `UPDATE` on the ticker row rather than a Postgres
advisory lock — same guarantee, no dialect-specific code path, works identically on the
SQLite used in tests, and self-heals if the holding process dies.

**Failure policy.** Prices are load-bearing; everything downstream is best-effort *per
movement*. A news timeout or an LLM error marks one movement `news_status: "failed"`,
adds a warning, and the run continues. A flaky news search never costs the caller its
price and movement data.

---

## 2. Is the solution satisfying?

Mostly yes, with one honest reservation.

**What I am satisfied with.** The movement definition is defensible and auditable —
you can see not just that a day was flagged but which term of the `max()` decided it,
and re-running with different parameters produces a different, equally explainable
answer. The three tiers genuinely work on real data: on D.R. Horton, a company-free
macro query surfaced a broad-market/oil article that never mentions the company, and
the model connected it to a +5.26% day through rate sensitivity. That is the case
keyword search structurally cannot reach, and it works.

The scoring step also demonstrably discriminates rather than rubber-stamping. On
Apple's -7.35% earnings day it rated Reuters' guidance-miss story 0.92 and Apple's *own*
press release 0.35, with the rationale "does not mention the weak guidance that drove
the sell-off". That is the distinction between an article that is *about* a company and
one that *explains* a move, which is the whole problem.

**The reservation: relevance quality is asserted, not measured.** I have no labelled
set of movement→true-cause pairs, so "the scoring is good" rests on my reading of a few
dozen outputs. Spot-checking is not evaluation. Everything else in the system has a
test; the component carrying the most judgement has none, and I would not ship this to
users without fixing that.

I am also not fully satisfied that a *univariate* movement definition is the right one
(see below).

---

## 3. What I would do differently with more time

**Evaluate the relevance scoring.** Build a labelled set from 50–100 movements where the
cause is publicly uncontroversial (major earnings days, known Fed dates), then measure
precision@k and tier-assignment accuracy. That turns prompt changes from taste into
measurement, and it is the single highest-value missing piece.

**Make movement detection market-relative.** The current definition is univariate: it
cannot tell "NVDA fell because NVDA" from "NVDA fell because everything fell". Regressing
each stock's return on a sector ETF and flagging on the *residual* would separate the
two — and would make the Hard tier sharper, since a day that is unremarkable after
removing the market's move probably does not need a company-specific explanation at all.
This is the change I would make first.

**Retrieve better in chat.** Retrieval is deliberately deterministic — every movement as
a one-line summary, the largest ones in full. That is correct for a few dozen movements
and will not scale to a multi-ticker corpus or to questions like "when has regulation
hurt this stock before". Embedding the articles with pgvector and doing hybrid search is
the obvious next step.

**Fetch full article text.** Scoring quality is bounded by what Exa returns, which is
usually a summary or the first ~2k characters and sometimes paywalled boilerplate.
Fetching and extracting the article body would give the scorer more to work with.

**Operational maturity.** A task queue (ARQ/Celery) rather than FastAPI `BackgroundTasks`
so ingestion survives a restart and can be retried with visibility; a scheduler to keep
tickers warm instead of making the first caller wait ~6 minutes; and per-request cost
accounting, since the system spends real money on every cold ticker.

---

## 4. Where I got stuck, and how I got unstuck

**yfinance silently returning nothing.** The first live run failed with "possibly
delisted; no timezone found" for NVDA, which is obviously wrong. The pinned version was
too old for Yahoo's current API and was getting a non-JSON response. Upgrading fixed it.
The lesson I kept: yfinance fails by returning an empty frame at least as often as it
raises, so the price service is written defensively and the rest of the app only ever
sees `PricePoint` objects and typed errors.

I also lost time to a stale process — I killed a server with `kill %1` in one shell and
restarted in another, not realising the old process still held the port, so I spent two
runs debugging code that was not the code being executed. Killing by port fixed it.

**The bug worth reporting: a confidently wrong explanation.** A live run linked an
article published `2026-07-30` to a movement on `2026-07-02` and the scorer rated it
**0.95** — a confident explanation for a move four weeks earlier. This is the worst
failure mode this system has, because it looks exactly like a good answer.

It took two passes to actually fix. The first cause was that Exa treats the
published-date filter as a hint and leaked an out-of-range result; I added a filter on
provider output. The out-of-window article *came back anyway*. The second cause was
subtler: the search returned that article with **no** publication date, so the filter
had nothing to test and passed it; deduplication then resolved it by URL to a row
already stored from a different movement, whose real date was four weeks away. The date
being checked and the date being stored were different dates.

The fix is to enforce the window against the **stored** article, which is the
authoritative record, immediately before writing the link — and to keep the provider
filter as a cheap pre-filter so obviously-out-of-window candidates never cost scoring
tokens. Both paths have regression tests.

The general lesson, which I applied elsewhere afterwards: **do not delegate a hard
constraint to a model or to a third party.** The date window is a requirement of the
product, not a preference, so it belongs in code. I had originally also tried to fix it
by strengthening the prompt; the prompt change is still there, but it is the belt, not
the braces.

**Two more found by writing tests and by reading my own code critically:**

- The three tier searches ran concurrently on a single `AsyncSession`, which is not safe
  for concurrent use. SQLAlchemy surfaced it as a warning during a test run rather than
  a failure, which is how it nearly got missed. The DB touches are now serialized by a
  lock while the HTTP calls still overlap.
- An unknown ticker returned **502** rather than 404, because yfinance raises before the
  empty-frame check and reports "symbol does not exist" and "the network broke" through
  the same exception type. The `404` handler I had written was unreachable. It now
  classifies on the message, with tests pinning both directions.

A general note on process: the two bugs I am least comfortable with — the out-of-window
article and the 404 — were both found by *running the thing against real data and
reading the output*, not by tests. The tests caught the concurrency bug and stopped
regressions, but they only test what I already thought to check.
