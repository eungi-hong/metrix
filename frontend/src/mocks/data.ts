/**
 * SAMPLE DATA for the mocked API (end-to-end tests, screenshots, local
 * demos). Fictional companies, fictional outlets, synthetic prices: none of
 * it is market data or real reporting. Deterministic, so tests and
 * screenshots are stable.
 */
import type {
  BenchmarkSeries, ChatResponse, MarketPulse, Movement, MovementCase, PriceBar, SymbolSearch, TickerDetail,
} from "../api/types";

export const AS_OF = "2026-10-16";

function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 2 ** 32;
  };
}

function gauss(r: () => number): number {
  return Math.sqrt(-2 * Math.log(r() + 1e-12)) * Math.cos(2 * Math.PI * r());
}

/** Weekdays ending on AS_OF (holidays ignored: sample data). */
export function sessionDates(count: number, end = AS_OF): string[] {
  const out: string[] = [];
  const d = new Date(`${end}T12:00:00Z`);
  while (out.length < count) {
    const day = d.getUTCDay();
    if (day !== 0 && day !== 6) out.unshift(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() - 1);
  }
  return out;
}

const DATES = sessionDates(260);

interface Company {
  symbol: string;
  name: string;
  sector: string;
  industry: string;
  start: number;
  vol: number;
  seed: number;
  /** index from the end -> [market, sector, company] daily parts */
  shocks: Record<number, [number, number, number]>;
}

export const COMPANIES: Company[] = [
  { symbol: "NOVA", name: "Nova Semiconductor (sample)", sector: "Technology", industry: "Semiconductors", start: 120, vol: 0.018, seed: 11,
    shocks: { 1: [0.004, 0.006, 0.071], 15: [-0.021, -0.018, -0.012], 34: [0.002, 0.003, -0.064], 60: [0.012, 0.024, 0.005], 102: [0.001, -0.002, 0.083], 150: [-0.03, -0.01, -0.02] } },
  { symbol: "ARCM", name: "Arcadia Microdevices (sample)", sector: "Technology", industry: "Semiconductors", start: 64, vol: 0.02, seed: 23,
    shocks: { 2: [0.004, 0.006, -0.058], 15: [-0.021, -0.02, -0.015], 77: [0.003, 0.002, 0.069] } },
  { symbol: "KITE", name: "Kite Motors (sample)", sector: "Consumer Cyclical", industry: "Auto Manufacturers", start: 210, vol: 0.026, seed: 37,
    shocks: { 1: [0.004, -0.001, -0.091], 44: [0.006, 0.01, 0.112], 120: [-0.004, 0.0, -0.074] } },
  { symbol: "LUME", name: "Lume Health (sample)", sector: "Healthcare", industry: "Biotechnology", start: 48, vol: 0.03, seed: 41,
    shocks: { 0: [0.001, 0.002, 0.118], 63: [0.0, -0.01, -0.081] } },
  { symbol: "BRIX", name: "Brix Materials (sample)", sector: "Basic Materials", industry: "Building Materials", start: 88, vol: 0.016, seed: 53,
    shocks: { 0: [0.001, -0.032, -0.006], 90: [0.0, 0.004, 0.052] } },
];

const MARKET_SHOCKS: Record<number, number> = { 0: 0.001, 1: 0.004, 2: 0.004, 15: -0.021, 150: -0.03 };

function series(seed: number, vol: number, start: number, parts: (i: number) => number): PriceBar[] {
  const r = rng(seed);
  let close = start;
  return DATES.map((date, i) => {
    const ret = i === 0 ? 0 : parts(i) + gauss(r) * vol * 0.35;
    const prev = close;
    close = prev * (1 + ret);
    const open = prev * (1 + ret * 0.55);
    const high = Math.max(open, close) * (1 + Math.abs(gauss(r)) * vol * 0.3);
    const low = Math.min(open, close) * (1 - Math.abs(gauss(r)) * vol * 0.3);
    return { date, open: round(open), high: round(high), low: round(low), close: round(close), adj_close: round(close), volume: Math.round(20e6 * (1 + Math.abs(ret) * 25) * (0.8 + r() * 0.4)) };
  });
}

const round = (v: number) => Math.round(v * 100) / 100;
const fromEnd = (i: number) => DATES.length - 1 - i;

function marketPart(i: number) { return MARKET_SHOCKS[fromEnd(i)] ?? 0.0004; }

export const BENCHMARKS: Record<string, PriceBar[]> = {
  SPY: series(5, 0.009, 560, (i) => marketPart(i)),
  XLK: series(7, 0.012, 230, (i) => marketPart(i) * 1.1 + (COMPANIES[0].shocks[fromEnd(i)]?.[1] ?? 0)),
  XLY: series(9, 0.012, 190, (i) => marketPart(i) * 1.05),
  XLV: series(13, 0.01, 140, (i) => marketPart(i) * 0.8),
  XLB: series(17, 0.011, 90, (i) => marketPart(i) + (fromEnd(i) === 0 ? -0.032 : 0)),
};

const SECTOR_ETF: Record<string, string> = { Technology: "XLK", "Consumer Cyclical": "XLY", Healthcare: "XLV", "Basic Materials": "XLB" };

const SOURCES = ["Example Wire", "Market Desk", "Sample Business Daily", "Demo Financial Times"];

function headline(company: Company, ret: number, part: "market" | "sector" | "company", k: number): { title: string; rationale: string; tier: "easy" | "medium" | "hard" } {
  const short = company.name.replace(" (sample)", "");
  if (part === "market") return { title: `Stocks ${ret < 0 ? "slide" : "rally"} as rate expectations shift`, rationale: `A market-wide ${ret < 0 ? "sell-off" : "rally"} moved most stocks that session.`, tier: "hard" };
  if (part === "sector") return { title: `${company.industry} stocks ${ret < 0 ? "fall" : "climb"} on an industry ${ret < 0 ? "pricing" : "demand"} report`, rationale: `Industry-wide news moved ${short}'s peers the same way.`, tier: "medium" };
  const up = ret > 0;
  const variants = up
    ? [`${short} beats on revenue and raises its outlook`, `${short} wins a multi-year supply contract`]
    : [`${short} cuts its full-year guidance`, `${short} discloses a delayed product launch`];
  return { title: variants[k % variants.length], rationale: up ? "Reports the results that the move followed, released after the prior close." : "Reports the guidance cut on the morning of the move.", tier: "easy" };
}

function movementsFor(company: Company, prices: PriceBar[]): Movement[] {
  return Object.entries(company.shocks).map(([back, [mkt, sec]], n) => {
    const i = fromEnd(Number(back));
    const bar = prices[i];
    const prev = prices[i - 1];
    const ret = bar.adj_close / prev.adj_close - 1;
    const alpha = 0.0003;
    const market = mkt * 1.15;
    const sector = sec * 0.9;
    const abnormal = ret - alpha - market - sector;
    const parts = { market: Math.abs(market), sector: Math.abs(sector), company: Math.abs(abnormal) };
    const dominant = (Object.entries(parts).sort((a, b) => b[1] - a[1])[0][0]) as "market" | "sector" | "company";
    const sar = abnormal / (company.vol * 1.02);
    const explained = Number(back) !== 34; // one move still waiting for evidence
    const date = bar.date;
    const prevDate = prev.date;
    const news = explained ? [0, 1, 2].map((k) => {
      const part = k === 0 ? dominant : k === 1 ? (dominant === "company" ? "sector" : "company") : "market";
      const h = headline(company, ret, part, n + k);
      const overnight = k === 0;
      return {
        article: { id: 1000 + n * 10 + k, url: `https://news.example.test/${company.symbol.toLowerCase()}/${date}/${k}`, title: h.title, source: SOURCES[(n + k) % SOURCES.length], author: null, published_at: overnight ? `${prevDate}T20:${String(20 + k).padStart(2, "0")}:00Z` : `${date}T${15 + k}:05:00Z`, summary: null },
        relevance_tier: h.tier, relevance_score: [0.92, 0.71, 0.48][k], rationale: h.rationale, search_tier: h.tier,
        timing: overnight ? "overnight" : "intraday", timing_precision: "time", role: (["primary", "contributing", "context"] as const)[k], scorer_version: "v2",
      };
    }) : [];
    const earnings = dominant === "company" && ret > 0;
    return {
      id: company.seed * 100 + n, date, daily_return: ret, daily_return_pct: ret * 100, direction: ret < 0 ? "down" : "up",
      prev_adj_close: prev.adj_close, adj_close: bar.adj_close, volume: bar.volume, threshold: Math.max(0.03, 2 * company.vol),
      threshold_source: 2 * company.vol > 0.03 ? "volatility" : "floor", rolling_std: company.vol, sigma_multiple: Math.abs(ret) / company.vol,
      detector_k: 2, detector_window: 20, detector_floor: 0.03, news_status: explained ? "complete" : "pending",
      news, trigger: Math.abs(ret) >= 0.03 ? (Math.abs(sar) >= 2.5 ? "both" : "raw") : "abnormal", qualifies: true,
      decomposition: { model: "market_sector", market_benchmark: "SPY", sector_benchmark: SECTOR_ETF[company.sector], estimation_sessions: 120,
        alpha, beta_market: 1.15, beta_sector: 0.9, r_squared: 0.48, sigma_e: company.vol, market, sector, expected: alpha + market + sector, abnormal, sar, dominant, note: null },
      decomposition_warning: null,
      legs: { gap: ret * 0.62, intraday: (1 + ret) / (1 + ret * 0.62) - 1 },
      volume_analysis: { relative_volume: 1 + Math.abs(ret) * 30, lookback_sessions: 20 },
      drift: Number(back) > 20 ? { car_5: abnormal * 0.08, car_20: abnormal * 0.15, label_5: "held", label_20: "continued" } : null,
      regime: { realized_vol: company.vol * 15.8, percentile: 62 + n * 3, window_sessions: 20 },
      filings: earnings ? [{ accession: `0009${company.seed}-26-0000${n}`, form: "8-K", items: ["2.02", "9.01"], events: ["results of operations (earnings)"], accepted_at: `${prevDate}T20:05:00Z`, url: "https://www.sec.gov/", timing: "overnight", timing_precision: "time", relevance_tier: "easy", relevance_score: 0.94, role: "primary", rationale: "The company's own earnings release, filed 5 minutes after the prior close." }] : [],
      evidence: explained ? { state: "explained", detail: "Linked evidence explains this move.", window_closes_at: null, expected_by: null }
        : { state: "queued", detail: "Queued for research.", window_closes_at: `${date}T20:00:00Z`, expected_by: `${AS_OF}T21:15:00Z` },
      evidence_abstained: explained ? false : null, abstain_reason: null,
    } as unknown as Movement;
  }).sort((a, b) => b.date.localeCompare(a.date));
}

const PRICES: Record<string, PriceBar[]> = Object.fromEntries(COMPANIES.map((c) => [c.symbol, series(c.seed, c.vol, c.start, (i) => {
  const s = c.shocks[fromEnd(i)];
  return s ? s[0] * 1.15 + s[1] * 0.9 + s[2] : marketPart(i) * 1.1;
})]));
export const MOVEMENTS: Record<string, Movement[]> = Object.fromEntries(COMPANIES.map((c) => [c.symbol, movementsFor(c, PRICES[c.symbol])]));

function benchmarkSeries(company: Company): BenchmarkSeries[] {
  const sectorSymbol = SECTOR_ETF[company.sector];
  return [["market", "SPY"], ["sector", sectorSymbol]].map(([role, symbol]) => ({
    role, symbol, base_date: DATES[0], points: BENCHMARKS[symbol].map((b) => ({ date: b.date, value: b.adj_close })),
  })) as BenchmarkSeries[];
}

export function tickerDetail(symbol: string): TickerDetail | null {
  const company = COMPANIES.find((c) => c.symbol === symbol);
  if (!company) return null;
  const movements = MOVEMENTS[symbol];
  return {
    status: "ready", message: null, job_id: null,
    ticker: { symbol, company_name: company.name, sector: company.sector, industry: company.industry, exchange: "NASDAQ", currency: "USD" },
    ingest_status: "complete", last_ingested_at: `${AS_OF}T21:20:00Z`,
    price_range: { start: DATES[0], end: AS_OF, bars: DATES.length }, prices: PRICES[symbol],
    filters: { start: null, end: null, min_magnitude: null, direction: null, tiers: null, triggers: null, dominant: null, min_sar: null, sort: "date", include_unqualified: false },
    pagination: { limit: 200, offset: 0, total: movements.length, returned: movements.length },
    movements, warnings: [], benchmarks: benchmarkSeries(company),
  } as unknown as TickerDetail;
}

export function movementCase(symbol: string, date: string): MovementCase | null {
  const detail = tickerDetail(symbol);
  const movement = detail?.movements.find((m) => m.date === date);
  if (!detail || !movement) return null;
  const all = detail.movements.map((m) => m.date).sort();
  const i = all.indexOf(date);
  const prevDate = DATES[DATES.indexOf(date) - 1];
  const nextDate = DATES[DATES.indexOf(date) + 1] ?? date;
  const peers = COMPANIES.filter((c) => c.symbol !== symbol && c.sector === COMPANIES.find((x) => x.symbol === symbol)!.sector);
  const ret = (bars: PriceBar[]) => { const k = bars.findIndex((b) => b.date === date); return k > 0 ? bars[k].adj_close / bars[k - 1].adj_close - 1 : null; };
  return {
    ticker: detail.ticker, movement,
    session: { previous: prevDate, prior_close: `${prevDate}T20:00:00Z`, open: `${date}T13:30:00Z`, close: `${date}T20:00:00Z`, next_open: `${nextDate}T13:30:00Z` },
    detection: {
      trigger: movement.trigger ?? "raw", qualifies: true, current_mode: "raw",
      raw: { daily_return: movement.daily_return, threshold: movement.threshold, threshold_source: movement.threshold_source, rolling_std: movement.rolling_std, k: 2, window: 20, floor: 0.03, passed: Math.abs(movement.daily_return) >= movement.threshold },
      abnormal: { sar: movement.decomposition?.sar ?? null, abnormal_return: movement.decomposition?.abnormal ?? null, k: 2.5, floor_basis: "return", floor: 0.03, passed: Math.abs(movement.decomposition?.sar ?? 0) >= 2.5 },
    },
    benchmarks: [{ symbol: "SPY", name: null, role: "market", daily_return: ret(BENCHMARKS.SPY) }, { symbol: movement.decomposition?.sector_benchmark ?? "XLK", name: null, role: "sector", daily_return: ret(BENCHMARKS[movement.decomposition?.sector_benchmark ?? "XLK"]) }],
    peers: [...peers.map((p) => ({ symbol: p.symbol, name: p.name, role: "competitor", daily_return: ret(PRICES[p.symbol]) })), { symbol: null, name: "Private Foundry Co. (sample)", role: "supplier", daily_return: null }],
    previous_date: i > 0 ? all[i - 1] : null, next_date: i < all.length - 1 ? all[i + 1] : null,
  } as unknown as MovementCase;
}

export function pulse(sessions: number): MarketPulse {
  const covered = DATES.slice(-sessions);
  const movers = COMPANIES.flatMap((c) => MOVEMENTS[c.symbol].filter((m) => covered.includes(m.date)).map((m) => ({
    symbol: c.symbol, company_name: c.name, date: m.date, daily_return: m.daily_return, abnormal_return: m.decomposition?.abnormal ?? null,
    sar: m.decomposition?.sar ?? null, dominant: m.decomposition?.dominant ?? null,
    explanation: m.news[0] ? `${m.news[0].article.title}: ${m.news[0].rationale}` : m.evidence?.detail ?? "",
    evidence: m.evidence?.state ?? "not_started",
  }))).sort((a, b) => Math.abs(b.sar ?? 0) - Math.abs(a.sar ?? 0));
  const total = (bars: PriceBar[]) => bars.at(-1)!.adj_close / bars.at(-1 - sessions)!.adj_close - 1;
  return {
    sessions: covered, movers,
    sectors: [
      { sector: "Market", symbol: "SPY", total_return: total(BENCHMARKS.SPY) },
      { sector: "Basic Materials", symbol: "XLB", total_return: total(BENCHMARKS.XLB) },
      { sector: "Consumer Cyclical", symbol: "XLY", total_return: total(BENCHMARKS.XLY) },
      { sector: "Energy", symbol: "XLE", total_return: null },
      { sector: "Healthcare", symbol: "XLV", total_return: total(BENCHMARKS.XLV) },
      { sector: "Technology", symbol: "XLK", total_return: total(BENCHMARKS.XLK) },
    ],
  } as MarketPulse;
}

export function search(q: string): SymbolSearch {
  const needle = q.toLowerCase();
  const results = [...COMPANIES.map((c) => ({ symbol: c.symbol, name: c.name, exchange: "NASDAQ", is_etf: false, has_data: true })),
    { symbol: "NOVR", name: "Novera Holdings (sample)", exchange: "NYSE", is_etf: false, has_data: false },
    { symbol: "COLD", name: "Coldwater Logistics (sample)", exchange: "NYSE", is_etf: false, has_data: false }]
    .filter((r) => r.symbol.toLowerCase().startsWith(needle) || (r.name ?? "").toLowerCase().includes(needle));
  return { query: q, results };
}

export function chatAnswer(question: string, conversationId: string): ChatResponse {
  const company = COMPANIES.find((c) => question.toUpperCase().includes(c.symbol)) ?? COMPANIES[0];
  const top = [...MOVEMENTS[company.symbol]].sort((a, b) => Math.abs(b.daily_return) - Math.abs(a.daily_return))[0];
  const article = top.news[0];
  const label = `M${top.id}`;
  const answer = `${company.symbol} ${top.daily_return < 0 ? "fell" : "rose"} ${Math.abs(top.daily_return * 100).toFixed(1)}% on ${top.date} [${label}]. `
    + `Most of it was company-specific: the breakdown puts ${(Math.abs(top.decomposition?.abnormal ?? 0) * 100).toFixed(1)} points on the company itself`
    + (article ? `, after ${(article.article.title ?? "the news").toLowerCase()} [A${article.article.id}].` : ".")
    + `\n\n| Part | Points |\n|---|---|\n| Market | ${((top.decomposition?.market ?? 0) * 100).toFixed(1)} |\n| Company | ${((top.decomposition?.abnormal ?? 0) * 100).toFixed(1)} |`;
  return {
    conversation_id: conversationId, ticker: company.symbol, answer, grounded: true, answered_by: "model", citations_removed: 0,
    tool_calls: [{ name: "get_movements", arguments: { symbol: company.symbol }, summary: `5 of ${MOVEMENTS[company.symbol].length} ${company.symbol} movements` }, { name: "get_movement_case", arguments: { symbol: company.symbol, date: top.date }, summary: `${company.symbol} ${top.date}: 3 articles, 1 filings` }],
    sources: {
      movements: [{ ref: label, movement_id: top.id, symbol: company.symbol, date: top.date, daily_return_pct: top.daily_return * 100, direction: top.direction }],
      articles: article ? [{ ref: `A${article.article.id}`, article_id: article.article.id, title: article.article.title, url: article.article.url, source: article.article.source, published_at: article.article.published_at?.slice(0, 10) ?? null, relevance_tier: article.relevance_tier, relevance_score: article.relevance_score }] : [],
      filings: [],
    },
    suggestions: [`Why did ${company.symbol} fall on its worst day?`, `How did ${company.symbol} do against the market over the last 3 months?`],
  } as unknown as ChatResponse;
}
