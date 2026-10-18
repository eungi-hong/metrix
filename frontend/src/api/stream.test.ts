import { describe, expect, it, vi } from "vitest";
import { parseEventStream, streamChat } from "./stream";

describe("server-sent events", () => {
  it("keep the unfinished tail and drop heartbeats", () => {
    const parsed = parseEventStream('retry: 3000\n\nevent: job\ndata: {"id":7}\n\n: ping\n\nevent: delta\ndata: {"text":"Hi');
    expect(parsed.events).toEqual([{ type: "job", data: { id: 7 } }]);
    expect(parsed.rest).toBe('event: delta\ndata: {"text":"Hi');
  });
  it("stream an answer to its final, checked form", async () => {
    const chunks = ['event: tool_call\ndata: {"name":"get_movements","arguments":{}}\n\n', 'event: delta\ndata: {"text":"Fell [M1]"}\n\nevent: do', 'ne\ndata: {"answer":"Fell [M1]","conversation_id":"c"}\n\n'];
    const body = new ReadableStream({ start(c) { chunks.forEach((x) => c.enqueue(new TextEncoder().encode(x))); c.close(); } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
    const seen: string[] = [];
    const done = await streamChat({ question: "q" }, { onDelta: (t) => seen.push(t), onToolCall: (n) => seen.push(`call:${n}`) });
    expect(seen).toEqual(["call:get_movements", "Fell [M1]"]);
    expect(done).toMatchObject({ answer: "Fell [M1]", conversation_id: "c" });
  });
  it("fail when the stream ends with an error", async () => {
    const body = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode('event: error\ndata: {"error":"internal_error"}\n\n')); c.close(); } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
    await expect(streamChat({ question: "q" }, {})).rejects.toThrow("internal_error");
  });
});
