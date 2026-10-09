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
    await new MetrixApiClient("http://localhost:8000", "mtx_test").sendChat({ ticker: "NVDA", question: "What moved?", conversationId: "conv-1" });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ ticker: "NVDA", question: "What moved?", conversation_id: "conv-1" });
  });

  it("surfaces quota responses with retry guidance", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: "rate_limited", detail: "Over the chat quota." }), { status: 429, headers: { "Retry-After": "45", "Content-Type": "application/json" } })));
    await expect(new MetrixApiClient("http://localhost:8000", "mtx_test").sendChat({ ticker: "NVDA", question: "Why?" })).rejects.toMatchObject({ kind: "quota", retryAfter: 45, status: 429 });
  });
});
