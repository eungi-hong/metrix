# A 90-second walkthrough

A click path and what to say, for a screen recording at 1440 px.

**Live site:** use the three showcase case files once they are picked (README, "Live demo"). Until then, the same path works on the sample data: `cd frontend && VITE_MSW=1 npm run dev`, with NOVA standing in for NVDA. Say that it is sample data if you record it.

## Before recording
- Use a fresh browser profile in the light theme, at 100% zoom, with the window at 1440 by 900.
- Open the home page, so the pulse is already loaded.
- Pick the company-driven showcase move. The script calls it NVDA on its date.

## Script

| Time | Do | Say |
|---|---|---|
| 0:00 | Home page, no clicks. | "Metrix answers one question: why did this stock move? These are the largest unusual moves of the last session, each with a one-line reason." |
| 0:08 | Press `/`, type `nv`, press Enter. | "Any US ticker, from the keyboard." |
| 0:13 | Ticker page. Hover a coloured marker on the chart. | "Every flagged move is marked. The colour is what drove it: the market, the sector, or the company itself." |
| 0:22 | Tick "SPY market" above the chart. | "Rebased against the market, you can see which moves were the stock's own." |
| 0:28 | Press `j` twice in the table, then Enter. | "Each move has a case file." |
| 0:32 | Case file. Point at the breakdown bars. | "Of this 8.7% move, about half a point was the market and half a point the sector. The rest, 7.6 points, was company-specific. The parts add up exactly." Read the real numbers off the page. |
| 0:42 | Point at "When the news broke". | "Here is when each piece of evidence came out relative to the session. The earnings 8-K was filed five minutes after the prior close, so it could explain the overnight gap. An article published after the close could not." |
| 0:52 | Point at the primary source card, then the evidence list. | "The company's own SEC filing comes first, then the news, ranked, each with its role and why it was judged relevant." |
| 1:00 | Click "Ask about this move". Type "Did AMD fall for the same reason?" and press Enter. | "Ask Metrix answers from the stored data only. It shows what it looked up." |
| 1:10 | While it streams, hover a citation chip. | "Every claim is cited. Citations it cannot back with a row it actually retrieved are removed before you see them." |
| 1:18 | Click the chip. | "And every citation opens its case file." |
| 1:22 | Open Methodology in the top bar. | "How it works, with its evaluation numbers and limitations, including what is not measured yet." |
| 1:30 | End. | |

## Do not say
- Accuracy figures for relevance scoring. None exist until the labelled set is built (docs/v2/RESULTS.md, "Pending").
- That `union` detection is on. It is built, but `raw` is the default.
- Production latency numbers. The performance numbers are synthetic-local.
