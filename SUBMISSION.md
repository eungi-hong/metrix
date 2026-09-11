# Submission

## 1. How I went about it

Started by unpacking the jargon in the brief and working out what the recommended tools
actually do. Two things came out of reading up on price movements:

- A fixed percentage threshold is unfair across stocks, so it's volatility adjusted:
  `|return| >= max(2%, k x rolling_std)`. The rolling window excludes the day being
  tested, otherwise a big move inflates its own threshold and hides itself.
- Use adjusted close, not close. Splits and dividends would otherwise show up as fake
  movements. NVDA's splits alone would produce a few a year.

Then architecture. The main call was persistence. News and LLM scoring calls are slow and
cost money, and this mattered most for chat: if every message took a long time the thing
just isn't usable. So, a proper ingestion system with a real schema on Postgres, and
conversation identifiers so chat history survives.

Then building, in this order: data model, news layer, ingestion, API, chat. I used an LLM
throughout.

## 2. Am I happy with it?

Mostly. The persistence layer earns its place, and the system is reasonably extendible:
threshold parameters are configurable and stored on each movement row, so old results stay
explainable after the config changes.

## 3. What I'd do with more time

Make movement detection market-relative. It's univariate now, so it can't tell "NVDA fell
because NVDA" from "NVDA fell because everything fell". Regressing against a sector ETF
and flagging on the residual would separate those, and would sharpen the Hard tier. First
thing I'd change.

After that: fetch full article text, since scoring is capped by Exa's summaries. And
operational maturity, meaning a real task queue instead of `BackgroundTasks`.

## 4. Where I got stuck

The worst bug was a confidently wrong answer: an article published 2026-07-30 linked to a
movement on 2026-07-02 and scored 0.95. It looks exactly like a good answer, which is what
makes it bad.

Two causes. Exa treats the published-date filter as a hint, so I filtered provider output.
The article came back anyway, because the search returned it with no publication date, so
the filter had nothing to check, and dedup then matched it by URL to a row stored from a
different movement four weeks off. The date I was checking and the date I was storing
weren't the same date. Fix: check the window against the stored article, which is the
authoritative record, right before writing the link, and keep the provider filter as a
cheap pre-filter. Both paths have regression tests.

The lesson: don't hand a hard constraint to a model or a third party. The date window is a
product requirement, not a preference, so it belongs in code. I'd first tried to fix it by
strengthening the prompt. That's still there, but it's the belt, not the braces.
