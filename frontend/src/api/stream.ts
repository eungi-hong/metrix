/**
 * Server-sent events over fetch (so headers can be sent, which EventSource
 * cannot): GET /events for job progress and data changes, and POST
 * /chat/stream for Ask Metrix.
 */
import { API_BASE_URL, errorFrom, headers } from "./client";
import type { ChatRequest, ChatResponse } from "./types";

export interface StreamEvent {
  type: string;
  data: Record<string, unknown>;
}

/** Complete events in `buffer`, and the unfinished tail. Comments (heartbeats)
 * and retry hints carry no event and are dropped; malformed data is skipped. */
export function parseEventStream(buffer: string): { events: StreamEvent[]; rest: string } {
  const blocks = buffer.replace(/\r\n?/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: StreamEvent[] = [];
  for (const block of blocks) {
    let type = "";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) type = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!type || !data.length) continue;
    try {
      events.push({ type, data: JSON.parse(data.join("\n")) as Record<string, unknown> });
    } catch {
      /* skipped */
    }
  }
  return { events, rest };
}

async function consume(response: Response, onEvent: (event: StreamEvent) => void): Promise<void> {
  if (!response.ok || !response.body) throw await errorFrom(response);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseEventStream(buffer);
    buffer = parsed.rest;
    parsed.events.forEach(onEvent);
  }
  parseEventStream(`${buffer}\n\n`).events.forEach(onEvent);
}

/** Follow one ticker until the server ends the stream (a few minutes) or `signal` aborts. */
export async function followTicker(symbol: string, onEvent: (event: StreamEvent) => void, signal?: AbortSignal): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/events?symbol=${encodeURIComponent(symbol)}`, {
    headers: headers({ Accept: "text/event-stream" }),
    signal,
  });
  await consume(response, onEvent);
}

export interface ChatStreamHandlers {
  onDelta?: (text: string) => void;
  onToolCall?: (name: string, args: Record<string, unknown>) => void;
  onToolResult?: (name: string, summary: string, rows: number) => void;
}

/** Ask a question with streaming. Resolves with the final, citation-checked answer. */
export async function streamChat(body: ChatRequest, handlers: ChatStreamHandlers, signal?: AbortSignal): Promise<ChatResponse> {
  const response = await fetch(`${API_BASE_URL}/chat/stream`, {
    method: "POST",
    headers: headers({ Accept: "text/event-stream", "Content-Type": "application/json" }),
    body: JSON.stringify(body),
    signal,
  });
  let final: ChatResponse | null = null;
  let failure: string | null = null;
  await consume(response, (event) => {
    if (event.type === "delta") handlers.onDelta?.(String(event.data.text ?? ""));
    else if (event.type === "tool_call") handlers.onToolCall?.(String(event.data.name), (event.data.arguments ?? {}) as Record<string, unknown>);
    else if (event.type === "tool_result") handlers.onToolResult?.(String(event.data.name), String(event.data.summary ?? ""), Number(event.data.rows ?? 0));
    else if (event.type === "done") final = event.data as unknown as ChatResponse;
    else if (event.type === "error") failure = String(event.data.error ?? "internal_error");
  });
  if (final) return final;
  throw new Error(failure ?? "The answer stream ended early.");
}
