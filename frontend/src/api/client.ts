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

export class MetrixApiClient {
  constructor(
    private readonly baseUrl: string,
    private readonly apiKey: string,
  ) {}

  private async request<T>(path: string, init?: RequestInit): Promise<{ data: T; status: number }> {
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl.replace(/\/$/, "")}${path}`, {
        ...init,
        headers: {
          Accept: "application/json",
          ...(this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {}),
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

  async getTicker(symbol: string, options: { refresh?: boolean } = {}): Promise<TickerResult> {
    const params = new URLSearchParams({ include_prices: "true", limit: "50" });
    if (options.refresh) params.set("refresh", "true");
    const result = await this.request<TickerDetail>(`/tickers/${encodeURIComponent(symbol)}?${params}`);
    return { data: normalizeTickerResponse(result.data), status: result.status };
  }

  async getJob(jobId: number): Promise<Job> {
    return (await this.request<Job>(`/jobs/${jobId}`)).data;
  }

  async sendChat(input: { ticker: string; question: string; conversationId?: string }): Promise<ChatResponse> {
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
