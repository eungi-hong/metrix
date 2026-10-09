import type { ChatResponse, Movement, PriceBar, TickerDetail } from "../api/types";

const eventReturns: Record<string, number> = {
  "2026-06-18": 5.84,
  "2026-07-09": -6.21,
  "2026-07-27": 4.49,
  "2026-08-13": -3.78,
};

function isoDate(date: Date) {
  return date.toISOString().slice(0, 10);
}

function makePrices(): PriceBar[] {
  const cursor = new Date(Date.UTC(2026, 5, 1));
  const bars: PriceBar[] = [];
  let close = 171.2;
  let day = 0;
  while (bars.length < 58) {
    const weekDay = cursor.getUTCDay();
    if (weekDay !== 0 && weekDay !== 6) {
      const date = isoDate(cursor);
      const drift = ((day * 17) % 11 - 5) / 1000;
      const move = eventReturns[date] ? eventReturns[date] / 100 : drift;
      const open = close;
      close = Math.round(open * (1 + move) * 100) / 100;
      bars.push({
        date,
        open,
        high: Math.round(Math.max(open, close) * 1.013 * 100) / 100,
        low: Math.round(Math.min(open, close) * 0.987 * 100) / 100,
        close,
        adj_close: close,
        volume: 27_000_000 + ((day * 1_391_737) % 18_000_000),
      });
      day += 1;
    }
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return bars;
}

const prices = makePrices();
const priceAt = (date: string) => prices.find((bar) => bar.date === date)?.adj_close ?? 0;

const companyArticle = {
  id: 301,
  url: "https://www.reuters.com/technology/nvidia-demo-result",
  title: "Nvidia lifts outlook as data centre demand remains resilient",
  source: "Reuters",
  author: "Example Reporter",
  published_at: "2026-06-17T20:15:00Z",
  summary: "Demo fixture article for local interface development.",
};

const movements: Movement[] = [
  {
    id: 91,
    date: "2026-08-13",
    daily_return: -0.0378,
    daily_return_pct: -3.78,
    direction: "down",
    prev_adj_close: Math.round(priceAt("2026-08-12") * 100) / 100,
    adj_close: priceAt("2026-08-13"),
    volume: 42_130_000,
    threshold: 0.028,
    threshold_source: "volatility",
    rolling_std: 0.014,
    sigma_multiple: 2.7,
    detector_k: 2,
    detector_window: 20,
    detector_floor: 0.02,
    news_status: "pending",
    news: [],
  },
  {
    id: 72,
    date: "2026-07-27",
    daily_return: 0.0449,
    daily_return_pct: 4.49,
    direction: "up",
    prev_adj_close: Math.round(priceAt("2026-07-24") * 100) / 100,
    adj_close: priceAt("2026-07-27"),
    volume: 50_300_000,
    threshold: 0.031,
    threshold_source: "volatility",
    rolling_std: 0.0155,
    sigma_multiple: 2.9,
    detector_k: 2,
    detector_window: 20,
    detector_floor: 0.02,
    news_status: "partial",
    news: [
      {
        article: {
          id: 302,
          url: "https://www.example.com/semiconductor-demand",
          title: "Chip-equipment outlook points to durable AI infrastructure demand",
          source: "Industry Wire",
          author: null,
          published_at: "2026-07-27T13:10:00Z",
          summary: "Demo fixture article for an industry evidence tier.",
        },
        relevance_tier: "medium",
        relevance_score: 0.76,
        rationale: "A constructive read-through from the semiconductor supply chain supported the move.",
        search_tier: "medium",
      },
    ],
  },
  {
    id: 51,
    date: "2026-07-09",
    daily_return: -0.0621,
    daily_return_pct: -6.21,
    direction: "down",
    prev_adj_close: Math.round(priceAt("2026-07-08") * 100) / 100,
    adj_close: priceAt("2026-07-09"),
    volume: 61_720_000,
    threshold: 0.031,
    threshold_source: "volatility",
    rolling_std: 0.0156,
    sigma_multiple: 3.98,
    detector_k: 2,
    detector_window: 20,
    detector_floor: 0.02,
    news_status: "complete",
    news: [
      {
        article: {
          id: 303,
          url: "https://www.example.com/ai-export-controls",
          title: "New export-control guidance pressures AI chip shares",
          source: "Market Ledger",
          author: "A. Chen",
          published_at: "2026-07-09T15:32:00Z",
          summary: "Demo fixture article for macro and regulatory context.",
        },
        relevance_tier: "hard",
        relevance_score: 0.88,
        rationale: "Regulatory uncertainty weighed on the broader AI semiconductor complex during the session.",
        search_tier: "hard",
      },
      {
        article: {
          id: 304,
          url: "https://www.example.com/cloud-spending",
          title: "Cloud spend commentary creates a cautious read-through for accelerators",
          source: "Tech Brief",
          author: null,
          published_at: "2026-07-09T11:05:00Z",
          summary: "Demo fixture article for company-specific context.",
        },
        relevance_tier: "easy",
        relevance_score: 0.71,
        rationale: "Investor concern over near-term customer digestion offered a direct company-relevant explanation.",
        search_tier: "easy",
      },
    ],
  },
  {
    id: 22,
    date: "2026-06-18",
    daily_return: 0.0584,
    daily_return_pct: 5.84,
    direction: "up",
    prev_adj_close: Math.round(priceAt("2026-06-17") * 100) / 100,
    adj_close: priceAt("2026-06-18"),
    volume: 58_160_000,
    threshold: 0.027,
    threshold_source: "volatility",
    rolling_std: 0.0135,
    sigma_multiple: 4.33,
    detector_k: 2,
    detector_window: 20,
    detector_floor: 0.02,
    news_status: "complete",
    news: [
      {
        article: companyArticle,
        relevance_tier: "easy",
        relevance_score: 0.94,
        rationale: "A stronger demand outlook gave investors a company-specific catalyst for the rally.",
        search_tier: "easy",
      },
      {
        article: {
          id: 305,
          url: "https://www.example.com/foundry-capex",
          title: "Foundry capex plans reinforce the accelerator buildout",
          source: "Semiconductor Daily",
          author: null,
          published_at: "2026-06-18T12:40:00Z",
          summary: "Demo fixture article for industry context.",
        },
        relevance_tier: "medium",
        relevance_score: 0.79,
        rationale: "Supplier commentary suggested the demand signal extended across the industry.",
        search_tier: "medium",
      },
      {
        article: {
          id: 306,
          url: "https://www.example.com/risk-appetite",
          title: "Equities rise as rate expectations support long-duration technology",
          source: "Global Markets",
          author: null,
          published_at: "2026-06-18T16:20:00Z",
          summary: "Demo fixture article for macro context.",
        },
        relevance_tier: "hard",
        relevance_score: 0.63,
        rationale: "A broader risk-on session likely amplified the stock-specific catalyst.",
        search_tier: "hard",
      },
    ],
  },
];

export const readyDemo: TickerDetail = {
  status: "ready",
  message: null,
  job_id: null,
  ticker: {
    symbol: "NVDA",
    company_name: "NVIDIA Corporation",
    sector: "Technology",
    industry: "Semiconductors",
    exchange: "NASDAQ",
    currency: "USD",
  },
  ingest_status: "complete",
  last_ingested_at: "2026-08-19T21:12:00Z",
  price_range: { start: prices[0].date, end: prices.at(-1)?.date ?? null, bars: prices.length },
  prices,
  filters: { start: null, end: null, min_magnitude: null, direction: null, tiers: null },
  pagination: { limit: 50, offset: 0, total: movements.length, returned: movements.length },
  movements,
  warnings: ["Demo data is local fixture data and is not live market information."],
};

export const queuedDemo: TickerDetail = {
  ...readyDemo,
  status: "ingesting",
  message: "First-time ingestion queued. Stored data will appear when the worker completes.",
  job_id: 308,
  prices: [],
  movements: [],
  price_range: { start: null, end: null, bars: 0 },
  pagination: { limit: 50, offset: 0, total: 0, returned: 0 },
};

export const emptyDemo: TickerDetail = {
  ...readyDemo,
  ticker: { ...readyDemo.ticker, symbol: "NEWD", company_name: "New Discovery Inc." },
  prices: [],
  movements: [],
  price_range: { start: null, end: null, bars: 0 },
  pagination: { limit: 50, offset: 0, total: 0, returned: 0 },
  warnings: [],
};

export const failedDemo: TickerDetail = {
  ...emptyDemo,
  status: "failed",
  message: "The last ingestion could not reach the upstream market-data service.",
};

export const demoChatResponse: ChatResponse = {
  conversation_id: "demo-nvda-001",
  ticker: "NVDA",
  answer: "The largest stored decline was -6.21% on 9 Jul. The evidence links both export-control uncertainty [A1] and cautious cloud-spend commentary [A2] to that session [M1].",
  grounded: true,
  sources: {
    movements: [{ ref: "M1", movement_id: 51, date: "2026-07-09", daily_return_pct: -6.21, direction: "down" }],
    articles: [
      { ref: "A1", article_id: 303, title: "New export-control guidance pressures AI chip shares", url: "https://www.example.com/ai-export-controls", source: "Market Ledger", published_at: "2026-07-09", relevance_tier: "hard", relevance_score: 0.88 },
      { ref: "A2", article_id: 304, title: "Cloud spend commentary creates a cautious read-through for accelerators", url: "https://www.example.com/cloud-spending", source: "Tech Brief", published_at: "2026-07-09", relevance_tier: "easy", relevance_score: 0.71 },
    ],
  },
};
