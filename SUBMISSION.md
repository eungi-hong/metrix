# Submission

## Briefly describe your process for completing the project from start to finish. Include details on assumptions made and key decisions taken / tradeoffs made for each step (if relevant).

I started by researching and clarifying the technical jargon and the ambiguity in the
instructions, because I wanted to be sure I understood what was being asked before I
committed to any structure.

From there I did a short piece of research on how stock price movements are actually
measured and on what yfinance gives you, and two things came out of it. The first is that
a fixed percentage threshold is not a fair comparison across stocks, since a three percent
day is routine for one company and extraordinary for another, so I made the threshold
volatility adjusted and took the rolling standard deviation into account, flagging a day
when the absolute return is at least the larger of two percent and k times that rolling
standard deviation. The rolling window excludes the day being tested, because otherwise a
large move inflates the very threshold it has to clear and quietly hides itself. The
second is that yfinance returns both close and adjusted close, and adjusted close is the
one that matters here, because it folds splits and dividends back into the series, so a
four for one split reads as an ordinary day rather than as a seventy five percent collapse.
NVDA's splits on their own would otherwise have produced several imaginary movements.

I then did the same kind of research on the tools and resources the instructions
recommended. I went with Exa for the news layer because its support for
semantic search suits this problem, where I know the shape of the event I am looking for
but not the words the article will use to describe it.

Next came the architecture, and the decision that shaped everything downstream was
persistence. News API calls and LLM relevance scoring calls are both slow and both cost
money on every run, and that weighed heaviest on chat, because a chat that has to fetch and
score before it can answer anything is a chat nobody will use. So I put an ingestion system
in front of the whole thing, with a proper schema on Postgres, and the expensive work is
done once at ingestion time and read back cheaply from then on. I also gave conversations
their own identifiers, so that chat history is a stored thing rather than something held in
memory for the lifetime of a process.

With that settled I built the system with an LLM doing a lot of the typing, working in
dependency order, starting with the data model, then the news layer, then ingestion, then
the API, and finally chat.

## Are you happy with your solution? Why or why not?

Broadly yes. The separation between the database, the ORM models, the services and the API makes the project extendible, and the threshold
parameters are configurable rather than hardcoded and are stored on each movement row, so it can be adjusted accoridngly.

The persistence decision also proved itself during the build rather than only on paper,
because the LLM scoring calls during ingestion turned out to take a very long time, and
had any of that work been left to request time the system would have been unusable.

## What would you do differently if you got to do this over again?

The first change would be to make movement detection market relative. As it stands the
detection is univariate, so it cannot distinguish a stock that fell because of something
specific to the company from a stock that fell because the entire market fell that day. If
I had more time I would do more research into how to measure this accurately, and the
approach I would start from is regressing the stock against a sector ETF and flagging on
the residual, which would also sharpen the macro tier considerably.

After that I would spend the time on operational maturity, which mostly means putting a
real task queue behind ingestion instead of leaning on FastAPI's `BackgroundTasks`. I would
also fetch the full article text rather than working from Exa's summaries, since the
quality of the scoring is capped by how much of the article the scorer gets to see.

## Did you get stuck anywhere? How'd you get unstuck?

The place I got properly stuck was the Hard tier, and specifically the question of scope and
of what you are even searching for. I worked through it by leaning on what yfinance already
gives me, since it returns a sector and an industry for each ticker, which is a real
grounding rather than something I would have had to invent. The risk with that grounding is
that a sector wide sweep is broad and produces a lot of overlap between companies sitting in
the same sector, so the shape of the query mattered more than usual.

That is where the fork was. The instinct is to search for something like "NVIDIA Federal
Reserve interest rates", and that instinct defeats the entire tier, because an article about
a Fed decision never mentions NVIDIA at all. If the company name is in the query then the
only articles you can retrieve are articles that mention the company, which is exactly the
set the Easy tier already covers, so the Hard tier only does any work if the query is
company free.

So I settled on a query that names the sector and nothing else, along the lines of
macroeconomic and political news moving Technology stocks, covering Fed decisions, inflation
data, tariffs, regulation and geopolitics. Whether a given macro event actually explains this
particular company's move is then left to the scorer, which is the right division of labour,
because that is a judgment call and a search index is not a judgment engine.

The useful side effect is that because the query contains no company, every ticker in the
same sector on the same date produces a byte identical query, and they all land on the same
cache row. A decision I made for correctness turned out to be the largest cost saving in the
system.

