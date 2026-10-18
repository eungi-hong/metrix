/**
 * The mocked Metrix API (MSW), over the sample data. Scenarios for tests:
 * symbol LIMIT answers 429 (the daily limit for new tickers); symbol COLD is
 * researched live: 202, progress over the event stream, then ready.
 */
import { delay, http, HttpResponse } from "msw";
import type { Watchlist, WatchlistIn } from "../api/types";
import { AS_OF, COMPANIES, MOVEMENTS, chatAnswer, movementCase, pulse, search, tickerDetail } from "./data";

const encoder = new TextEncoder();

function sse(events: [string, unknown][], gapMs = 60): HttpResponse<ReadableStream> {
  const stream = new ReadableStream({
    async start(controller) {
      controller.enqueue(encoder.encode("retry: 3000\n\n"));
      for (const [event, data] of events) {
        await delay(gapMs);
        controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      }
      controller.close();
    },
  });
  return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" } });
}

const limited = () => HttpResponse.json(
  { error: "rate_limited", detail: "Over the cold_ingests_per_day quota (3); retry in 3600 s." },
  { status: 429, headers: { "Retry-After": "3600", "X-RateLimit-Reset": "3600", "X-RateLimit-Limit": "3", "X-RateLimit-Remaining": "0", "X-RateLimit-Scope": "cold_ingests_per_day", "X-Request-ID": "req-sample-429" } },
);

let coldReady = false;
let nextId = 1;
const conversations: { id: string; ticker: string | null; created_at: string; messages: { role: string; content: string; sources: unknown }[] }[] = [];
const watchlists: Watchlist[] = [];

function coldDetail() {
  const base = tickerDetail("KITE")!;
  return { ...base, ticker: { ...base.ticker, symbol: "COLD", company_name: "Coldwater Logistics (sample)" } };
}

export function resetMocks() {
  coldReady = false;
  conversations.length = 0;
  watchlists.length = 0;
}

export const handlers = [
  http.get("*/market/pulse", ({ request }) => HttpResponse.json(pulse(Number(new URL(request.url).searchParams.get("sessions") ?? 1)))),
  http.get("*/symbols/search", ({ request }) => HttpResponse.json(search(new URL(request.url).searchParams.get("q") ?? ""))),

  http.get("*/tickers/:symbol/movements/:date", ({ params }) => {
    const body = movementCase(String(params.symbol).toUpperCase(), String(params.date));
    return body ? HttpResponse.json(body) : HttpResponse.json({ error: "not_found", detail: `${params.symbol} has no movement on ${params.date}.` }, { status: 404 });
  }),
  http.get("*/tickers/:symbol/earnings", ({ params }) => {
    const symbol = String(params.symbol).toUpperCase();
    const events = (MOVEMENTS[symbol] ?? []).flatMap((m) => m.filings.filter((f) => f.items.includes("2.02")).map((f) => ({
      accession: f.accession, accepted_at: f.accepted_at, filing_date: m.date, session: m.date, timing: "after_close", url: f.url,
      movement: { date: m.date, daily_return: m.daily_return, abnormal_return: m.decomposition?.abnormal ?? null, evidence: "explained" },
    })));
    return HttpResponse.json({ symbol, source: "sec_8k_item_2.02", events, note: "Sample data." });
  }),
  http.get("*/tickers/:symbol", async ({ params }) => {
    const symbol = String(params.symbol).toUpperCase();
    if (symbol === "LIMIT") return limited();
    if (symbol === "COLD") {
      if (coldReady) return HttpResponse.json(coldDetail());
      const empty = { ...coldDetail(), status: "ingesting", job_id: 77, prices: [], movements: [], benchmarks: [], last_ingested_at: null, message: "Researching COLD." };
      return HttpResponse.json(empty, { status: 202 });
    }
    const body = tickerDetail(symbol);
    return body ? HttpResponse.json(body) : HttpResponse.json({ error: "not_found", detail: `'${symbol}' is not a listed US symbol.` }, { status: 404 });
  }),

  http.get("*/events", ({ request }) => {
    const symbol = new URL(request.url).searchParams.get("symbol");
    if (symbol !== "COLD" || coldReady) return sse([], 0);
    const job = (status: string, progress: unknown) => ["job", { id: 77, kind: "ingest_ticker", status, progress }] as [string, unknown];
    const events: [string, unknown][] = [
      job("running", { stage: "prices" }),
      job("running", { stage: "detected", sessions: 260, movements: 3 }),
      job("running", { stage: "enriching", done: 1, total: 3, sessions: 260, movements: 3 }),
      job("running", { stage: "enriching", done: 3, total: 3, sessions: 260, movements: 3 }),
    ];
    const stream = new ReadableStream({
      async start(controller) {
        for (const [event, data] of events) {
          await delay(250);
          controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
        }
        coldReady = true;
        controller.enqueue(encoder.encode(`event: job\ndata: ${JSON.stringify({ id: 77, kind: "ingest_ticker", status: "succeeded", progress: { stage: "enriching", done: 3, total: 3 } })}\n\n`));
        controller.close();
      },
    });
    return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
  }),
  http.get("*/jobs/:id", () => HttpResponse.json({ id: 77, kind: "ingest_ticker", status: coldReady ? "succeeded" : "running", progress: { stage: "prices" }, last_error: null })),

  http.post("*/chat/stream", async ({ request }) => {
    const body = (await request.json()) as { question: string; conversation_id?: string };
    const id = body.conversation_id ?? `conv-${nextId++}`;
    const answer = chatAnswer(body.question, id);
    let conversation = conversations.find((c) => c.id === id);
    if (!conversation) {
      conversation = { id, ticker: answer.ticker, created_at: `${AS_OF}T22:00:00Z`, messages: [] };
      conversations.unshift(conversation);
    }
    conversation.messages.push({ role: "user", content: body.question, sources: null }, { role: "assistant", content: answer.answer, sources: answer.sources });
    const words = answer.answer.split(/(?<= )/);
    return sse([
      ["tool_call", { name: "get_movements", arguments: { symbol: answer.ticker } }],
      ["tool_result", { name: "get_movements", summary: answer.tool_calls[0].summary, rows: 5 }],
      ["tool_call", { name: "get_movement_case", arguments: {} }],
      ["tool_result", { name: "get_movement_case", summary: answer.tool_calls[1].summary, rows: 1 }],
      ...words.map((w) => ["delta", { text: w }] as [string, unknown]),
      ["done", answer],
    ], 15);
  }),
  http.get("*/conversations", () => HttpResponse.json({
    conversations: conversations.map((c) => ({ id: c.id, ticker: c.ticker, created_at: c.created_at, last_message_at: c.created_at, messages: c.messages.length })),
    limit: 30, offset: 0, total: conversations.length,
  })),
  http.get("*/conversations/:id", ({ params }) => {
    const c = conversations.find((x) => x.id === params.id);
    return c
      ? HttpResponse.json({ id: c.id, ticker: c.ticker, created_at: c.created_at, messages: c.messages.map((m, i) => ({ id: i + 1, role: m.role, content: m.content, sources: m.sources, created_at: c.created_at })) })
      : HttpResponse.json({ error: "not_found", detail: "No such conversation." }, { status: 404 });
  }),

  http.get("*/watchlists", () => HttpResponse.json(watchlists)),
  http.post("*/watchlists", async ({ request }) => {
    const body = (await request.json()) as WatchlistIn;
    const list = toWatchlist(watchlists.length + 1, body);
    watchlists.push(list);
    return HttpResponse.json(list, { status: 201 });
  }),
  http.put("*/watchlists/:id", async ({ params, request }) => {
    const i = watchlists.findIndex((w) => w.id === Number(params.id));
    watchlists[i] = toWatchlist(Number(params.id), (await request.json()) as WatchlistIn);
    return HttpResponse.json(watchlists[i]);
  }),
  http.delete("*/watchlists/:id", ({ params }) => {
    const i = watchlists.findIndex((w) => w.id === Number(params.id));
    if (i >= 0) watchlists.splice(i, 1);
    return new HttpResponse(null, { status: 204 });
  }),
  http.get("*/watchlists/:id/digest", ({ params, request }) => {
    const list = watchlists.find((w) => w.id === Number(params.id));
    const sessions = Number(new URL(request.url).searchParams.get("sessions") ?? 5);
    if (!list) return HttpResponse.json({ error: "not_found", detail: "No such watchlist." }, { status: 404 });
    const holdings = list.holdings.map((h) => {
      const detail = tickerDetail(h.symbol);
      const prices = detail?.prices ?? [];
      const r = prices.length > sessions ? prices.at(-1)!.adj_close / prices.at(-1 - sessions)!.adj_close - 1 : null;
      const recent = (MOVEMENTS[h.symbol] ?? []).filter((m) => prices.slice(-sessions).some((p) => p.date === m.date));
      return { symbol: h.symbol, weight: h.weight, has_data: Boolean(detail), period_return: r, contribution: r == null ? null : r * h.weight,
        moves: recent.map((m) => ({ date: m.date, daily_return: m.daily_return, abnormal_return: m.decomposition?.abnormal ?? null, evidence: m.evidence?.state ?? "not_started", explanation: m.news[0]?.article.title ?? "" })) };
    });
    const sum = holdings.reduce((s, h) => s + (h.contribution ?? 0), 0);
    return HttpResponse.json({ id: list.id, name: list.name, sessions: tickerDetail("NOVA")!.prices!.slice(-sessions).map((p) => p.date), period_return: sum, holdings, missing: holdings.filter((h) => !h.has_data).map((h) => h.symbol) });
  }),
];

function toWatchlist(id: number, body: WatchlistIn): Watchlist {
  const holdings = body.holdings ?? [];
  const weighted = holdings.length > 0 && holdings.every((h) => h.weight != null);
  const total = weighted ? holdings.reduce((s, h) => s + (h.weight ?? 0), 0) : holdings.length;
  return {
    id, name: body.name, weighted, created_at: `${AS_OF}T22:00:00Z`, updated_at: `${AS_OF}T22:00:00Z`,
    holdings: holdings.map((h) => ({ symbol: h.symbol.toUpperCase(), weight: weighted ? (h.weight ?? 0) / total : 1 / total })),
  } as Watchlist;
}

export { COMPANIES };
