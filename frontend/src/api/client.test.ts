import { describe, expect, it, vi } from "vitest";
import { MetrixApiClient, normalizeTickerResponse } from "./client";
import { readyDemo } from "../fixtures/demo";

describe("ticker response normalisation", () => {
  it("keeps API meanings and makes optional collections renderable", () => {
    const result = normalizeTickerResponse({ ...readyDemo, prices: null, warnings: undefined as unknown as string[], movements: [{ ...readyDemo.movements[0], news: undefined as unknown as [] }] });
    expect(result.status).toBe("ready");
    expect(result.prices).toEqual([]);
    expect(result.warnings).toEqual([]);
    expect(result.movements[0].news).toEqual([]);
  });
});

describe("MetrixApiClient", () => {
  it("reuses a supplied conversation id on a chat turn", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ conversation_id: "conv-1", ticker: "NVDA", answer: "Stored evidence.", grounded: true, sources: { movements: [], articles: [] } }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await new MetrixApiClient("http://localhost:8000", { exaApiKey: "exa_test", openaiApiKey: "sk-test" }).sendChat({ ticker: "NVDA", question: "What moved?", conversationId: "conv-1" });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ ticker: "NVDA", question: "What moved?", conversation_id: "conv-1" });
    expect(fetchMock.mock.calls[0][1].headers).toMatchObject({ "X-Exa-Api-Key": "exa_test", "X-OpenAI-Api-Key": "sk-test" });
  });

  it("allows a standalone chat turn without a selected ticker", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ conversation_id: "conv-1", ticker: null, answer: "No stored evidence.", grounded: false, sources: { movements: [], articles: [] } }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await new MetrixApiClient("http://localhost:8000", { exaApiKey: "exa_test", openaiApiKey: "sk-test" }).sendChat({ question: "What market themes matter this week?" });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ question: "What market themes matter this week?" });
  });

  it("surfaces quota responses with retry guidance", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: "rate_limited", detail: "Over the chat quota." }), { status: 429, headers: { "Retry-After": "45", "Content-Type": "application/json" } })));
    await expect(new MetrixApiClient("http://localhost:8000", { exaApiKey: "exa_test", openaiApiKey: "sk-test" }).sendChat({ ticker: "NVDA", question: "Why?" })).rejects.toMatchObject({ kind: "quota", retryAfter: 45, status: 429 });
  });
});

describe("event stream", () => {
  it("parses complete events, drops heartbeats and keeps the unfinished tail", async () => {
    const { parseEventStream } = await import("./client");
    const parsed = parseEventStream('retry: 3000\n\nevent: job\ndata: {"id":7,"status":"running"}\n\n: ping\n\nevent: ticker_updated\ndata: {"symbol":"NVDA","data_version":3}\n\nevent: job\ndata: {"id":');
    expect(parsed.events).toEqual([
      { type: "job", data: { id: 7, status: "running" } },
      { type: "ticker_updated", data: { symbol: "NVDA", data_version: 3 } },
    ]);
    expect(parsed.rest).toBe('event: job\ndata: {"id":');
  });

  it("delivers events across chunk boundaries", async () => {
    const chunks = ['event: job\ndata: {"id":1,', '"status":"succeeded"}\n\n'];
    const body = new ReadableStream({ start(controller) { chunks.forEach((c) => controller.enqueue(new TextEncoder().encode(c))); controller.close(); } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } })));
    const seen: unknown[] = [];
    await new MetrixApiClient("http://localhost:8000").streamEvents("NVDA", (event) => seen.push(event));
    expect(seen).toEqual([{ type: "job", data: { id: 1, status: "succeeded" } }]);
  });

  it("rejects when the stream cannot be opened, so the caller can poll instead", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "busy" }), { status: 503, headers: { "Retry-After": "5" } })));
    await expect(new MetrixApiClient("http://localhost:8000").streamEvents("NVDA", () => undefined)).rejects.toMatchObject({ status: 503 });
  });

  it("revalidates ticker data instead of trusting the browser's fresh copy", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(readyDemo), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await new MetrixApiClient("http://localhost:8000").getTicker("NVDA");
    expect(fetchMock.mock.calls[0][1].cache).toBe("no-cache");
  });
});
