import type { ChatResponse, Job, TickerDetail } from "./types";

export type ApiErrorKind =
  | "credentials"
  | "quota"
  | "spend-cap"
  | "upstream"
  | "network"
  | "request";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly kind: ApiErrorKind,
    readonly status?: number,
    readonly retryAfter?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export interface TickerResult {
  data: TickerDetail;
  status: number;
}

export interface ProviderKeys {
  exaApiKey: string;
  openaiApiKey: string;
}

export function normalizeTickerResponse(payload: TickerDetail): TickerDetail {
  // The API intentionally nests evidence under its movement. Only normalise
  // optional collections so rendering never has to invent missing data.
  return {
    ...payload,
    prices: payload.prices ?? [],
    movements: payload.movements.map((movement) => ({
      ...movement,
      news: movement.news ?? [],
    })),
    warnings: payload.warnings ?? [],
  };
}

function errorFromResponse(status: number, body: unknown, retryAfter: string | null) {
  const record = typeof body === "object" && body !== null ? body as Record<string, unknown> : {};
  const message = typeof record.detail === "string" ? record.detail : `Request failed (${status}).`;
  const retry = retryAfter ? Number(retryAfter) : undefined;
  const code = record.error;
  const kind: ApiErrorKind = status === 401
    ? "credentials"
    : status === 429
      ? "quota"
      : status === 503 && code === "spend_cap_reached"
        ? "spend-cap"
        : status >= 500
          ? "upstream"
          : "request";
  return new ApiError(message, kind, status, Number.isFinite(retry) ? retry : undefined);
}

/** One server-sent event from GET /events. */
export interface StreamEvent {
  type: "job" | "ticker_updated";
  data: Record<string, unknown>;
}

/**
 * Split a text/event-stream buffer into complete events and the unfinished tail.
 * Comment lines (heartbeats) and `retry:` hints carry no event and are dropped.
 */
export function parseEventStream(buffer: string): { events: StreamEvent[]; rest: string } {
  const normalized = buffer.replace(/\r\n?/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: StreamEvent[] = [];
  for (const block of blocks) {
    let type = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) type = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if ((type === "job" || type === "ticker_updated") && data.length) {
      try { events.push({ type, data: JSON.parse(data.join("\n")) as Record<string, unknown> }); } catch { /* a malformed event is skipped */ }
    }
  }
  return { events, rest };
}

export class MetrixApiClient {
  constructor(
    private readonly baseUrl: string,
    private readonly providerKeys?: ProviderKeys,
  ) {}

  private async request<T>(path: string, init?: RequestInit): Promise<{ data: T; status: number }> {
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl.replace(/\/$/, "")}${path}`, {
        ...init,
        headers: {
          Accept: "application/json",
          ...(this.providerKeys ? {
            "X-Exa-Api-Key": this.providerKeys.exaApiKey,
            "X-OpenAI-Api-Key": this.providerKeys.openaiApiKey,
          } : {}),
          ...init?.headers,
        },
      });
    } catch {
      throw new ApiError("Metrix could not reach the local service. Check that the API is running.", "network");
    }

    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) throw errorFromResponse(response.status, body, response.headers.get("Retry-After"));
    return { data: body as T, status: response.status };
  }

  async getTicker(symbol: string, options: { refresh?: boolean; wait?: boolean } = {}): Promise<TickerResult> {
    const params = new URLSearchParams({ include_prices: "true", limit: "50" });
    if (options.refresh) params.set("refresh", "true");
    if (options.wait) params.set("wait", "true");
    // no-cache: revalidate with the ETag every time (a 304 is cheap) rather
    // than reuse a copy the browser still considers fresh after an update.
    const result = await this.request<TickerDetail>(`/tickers/${encodeURIComponent(symbol)}?${params}`, { cache: "no-cache" });
    return { data: normalizeTickerResponse(result.data), status: result.status };
  }

  /**
   * Follow GET /events for one ticker until the server ends the stream (after a
   * few minutes) or `signal` aborts. Read with fetch rather than EventSource so
   * headers can be sent. Rejects if the stream cannot be opened.
   */
  async streamEvents(symbol: string, onEvent: (event: StreamEvent) => void, signal?: AbortSignal): Promise<void> {
    const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}/events?symbol=${encodeURIComponent(symbol)}`, {
      headers: { Accept: "text/event-stream" },
      signal,
    });
    if (!response.ok || !response.body) {
      throw errorFromResponse(response.status, await response.json().catch(() => null), response.headers.get("Retry-After"));
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseEventStream(buffer);
      buffer = parsed.rest;
      parsed.events.forEach(onEvent);
    }
  }

  async getJob(jobId: number): Promise<Job> {
    return (await this.request<Job>(`/jobs/${jobId}`)).data;
  }

  async sendChat(input: { ticker?: string; question: string; conversationId?: string }): Promise<ChatResponse> {
    return (await this.request<ChatResponse>("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: input.ticker,
        question: input.question,
        conversation_id: input.conversationId,
      }),
    })).data;
  }
}
