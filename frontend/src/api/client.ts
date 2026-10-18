/**
 * The Metrix API client. Every request goes through `request`, which turns a
 * non-2xx answer into an `ApiError` carrying the server's error code, the
 * limit it hit, when to retry and the request id, so `describeError` can
 * explain it in plain words.
 */
import type {
  ChatRequest, ChatResponse, ConversationDetail, ConversationList, EarningsOut, Job, MarketPulse,
  MovementCase, SymbolSearch, TickerDetail, TickerFilings, Watchlist, WatchlistDigest, WatchlistIn,
} from "./types";

export const API_BASE_URL: string = (import.meta.env.VITE_METRIX_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");

export interface ProviderKeys {
  exaApiKey: string;
  openaiApiKey: string;
}

let providerKeys: ProviderKeys | null = null;
/** Bring-your-own keys, from Settings. Sent only as request headers. */
export function setProviderKeys(keys: ProviderKeys | null) {
  providerKeys = keys;
}
export function getProviderKeys(): ProviderKeys | null {
  return providerKeys;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string | null,
    readonly scope: string | null = null,
    readonly retryAfterSeconds: number | null = null,
    readonly resetSeconds: number | null = null,
    readonly requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function headers(extra?: HeadersInit): Record<string, string> {
  return {
    Accept: "application/json",
    ...(providerKeys ? { "X-Exa-Api-Key": providerKeys.exaApiKey, "X-OpenAI-Api-Key": providerKeys.openaiApiKey } : {}),
    ...(extra as Record<string, string> | undefined),
  };
}

export async function errorFrom(response: Response): Promise<ApiError> {
  const body: unknown = await response.json().catch(() => null);
  const record = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  const number = (name: string) => {
    const value = Number(response.headers.get(name));
    return response.headers.get(name) != null && Number.isFinite(value) ? value : null;
  };
  return new ApiError(
    typeof record.detail === "string" ? record.detail : `Request failed (${response.status}).`,
    response.status,
    typeof record.error === "string" ? record.error : null,
    response.headers.get("X-RateLimit-Scope"),
    number("Retry-After"),
    number("X-RateLimit-Reset"),
    response.headers.get("X-Request-ID"),
  );
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { ...init, headers: headers(init?.headers) });
  } catch {
    throw new ApiError("network", 0, "network");
  }
  if (!response.ok) throw await errorFrom(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

type Params = Record<string, string | number | boolean | null | undefined | (string | number)[]>;

export function query(params: Params): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value == null || value === "" || value === false) continue;
    for (const item of Array.isArray(value) ? value : [value]) search.append(key, String(item));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

const json = (body: unknown): RequestInit => ({ body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });

export const api = {
  /** cache: no-cache revalidates with the ETag (a cheap 304) instead of trusting a stale copy. */
  ticker: (symbol: string, params: Params = {}) =>
    request<TickerDetail>(`/tickers/${encodeURIComponent(symbol)}${query({ include_prices: true, include_benchmarks: true, limit: 200, ...params })}`, { cache: "no-cache" }),
  movementCase: (symbol: string, day: string) =>
    request<MovementCase>(`/tickers/${encodeURIComponent(symbol)}/movements/${day}`, { cache: "no-cache" }),
  filings: (symbol: string) => request<TickerFilings>(`/tickers/${encodeURIComponent(symbol)}/filings`),
  earnings: (symbol: string) => request<EarningsOut>(`/tickers/${encodeURIComponent(symbol)}/earnings`),
  pulse: (sessions = 1, limit = 10) => request<MarketPulse>(`/market/pulse${query({ sessions, limit })}`),
  searchSymbols: (q: string, signal?: AbortSignal) => request<SymbolSearch>(`/symbols/search${query({ q, limit: 8 })}`, { signal }),
  job: (id: number) => request<Job>(`/jobs/${id}`),
  chat: (body: ChatRequest) => request<ChatResponse>("/chat", { method: "POST", ...json(body) }),
  conversations: () => request<ConversationList>(`/conversations${query({ limit: 30 })}`),
  conversation: (id: string) => request<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`),
  watchlists: () => request<Watchlist[]>("/watchlists"),
  createWatchlist: (body: WatchlistIn) => request<Watchlist>("/watchlists", { method: "POST", ...json(body) }),
  replaceWatchlist: (id: number, body: WatchlistIn) => request<Watchlist>(`/watchlists/${id}`, { method: "PUT", ...json(body) }),
  deleteWatchlist: (id: number) => request<void>(`/watchlists/${id}`, { method: "DELETE" }),
  digest: (id: number, sessions: number) => request<WatchlistDigest>(`/watchlists/${id}/digest${query({ sessions })}`),
};
