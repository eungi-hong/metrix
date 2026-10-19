/** Server state through TanStack Query: cached, retried where retrying can help, and revalidated with ETags. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "./client";
import { followTicker } from "./stream";
import type { Job, TickerDetail } from "./types";

// Retrying a 4xx (a limit, a bad symbol, nothing stored) cannot help.
export const retryable = (count: number, error: unknown) =>
  count < 2 && !(error instanceof ApiError && error.status >= 400 && error.status < 500);

export const keys = {
  ticker: (symbol: string) => ["ticker", symbol] as const,
  movementCase: (symbol: string, day: string) => ["case", symbol, day] as const,
  pulse: (sessions: number) => ["pulse", sessions] as const,
  search: (q: string) => ["search", q] as const,
  conversations: ["conversations"] as const,
  conversation: (id: string) => ["conversation", id] as const,
  watchlists: ["watchlists"] as const,
  digest: (id: number, sessions: number) => ["digest", id, sessions] as const,
  filings: (symbol: string) => ["filings", symbol] as const,
  earnings: (symbol: string) => ["earnings", symbol] as const,
};

export function useTicker(symbol: string) {
  return useQuery({ queryKey: keys.ticker(symbol), queryFn: () => api.ticker(symbol), enabled: Boolean(symbol), retry: retryable, staleTime: 30_000 });
}

export function useMovementCase(symbol: string, day: string) {
  return useQuery({ queryKey: keys.movementCase(symbol, day), queryFn: () => api.movementCase(symbol, day), retry: retryable, staleTime: 60_000 });
}

export function usePulse(sessions = 1) {
  return useQuery({ queryKey: keys.pulse(sessions), queryFn: () => api.pulse(sessions, 12), retry: retryable, staleTime: 60_000 });
}

export function useEarnings(symbol: string) {
  return useQuery({ queryKey: keys.earnings(symbol), queryFn: () => api.earnings(symbol), retry: false, staleTime: 300_000 });
}

/** Whether a ticker response is still being built. */
export function isWorking(data: TickerDetail | undefined): boolean {
  return Boolean(data && (data.status === "ingesting" || (data.status === "refreshing" && data.job_id)));
}

/**
 * Live updates for a ticker while work is running: job progress from the
 * event stream, and a refetch when the job finishes or the data changes. If
 * the stream cannot be opened, the job is polled every 3.5 s instead.
 */
export function useLiveTicker(symbol: string, data: TickerDetail | undefined) {
  const client = useQueryClient();
  const [job, setJob] = useState<Pick<Job, "id" | "status" | "progress" | "last_error"> | null>(null);
  const jobId = data?.job_id ?? null;
  const working = isWorking(data);
  const settled = useRef(false);

  useEffect(() => {
    if (!working || !jobId) {
      setJob(null);
      return;
    }
    settled.current = false;
    const abort = new AbortController();
    let poll: number | undefined;
    const refresh = () => {
      void client.invalidateQueries({ queryKey: keys.ticker(symbol) });
      void client.invalidateQueries({ queryKey: ["case", symbol] });
    };
    const settle = (status: string) => {
      if (settled.current) return;
      if (status === "succeeded" || status === "dead" || status === "failed") {
        settled.current = true;
        refresh();
        abort.abort();
        window.clearInterval(poll);
      }
    };
    const startPolling = () => {
      if (poll !== undefined || abort.signal.aborted) return;
      const tick = async () => {
        try {
          const latest = await api.job(jobId);
          setJob(latest);
          settle(latest.status);
        } catch { /* keep the last known state */ }
      };
      void tick();
      poll = window.setInterval(() => void tick(), 3500);
    };
    const follow = async () => {
      while (!abort.signal.aborted && !settled.current) {
        await followTicker(symbol, (event) => {
          if (event.type === "job" && event.data.id === jobId) {
            const update = event.data as unknown as Job;
            setJob((current) => ({ ...(current ?? { id: jobId, last_error: null }), status: update.status, progress: update.progress ?? current?.progress ?? null }));
            settle(update.status);
          } else if (event.type === "ticker_updated") {
            refresh();
          }
        }, abort.signal);
      }
    };
    follow().catch(() => startPolling());
    return () => {
      abort.abort();
      window.clearInterval(poll);
    };
  }, [client, jobId, symbol, working]);

  return job;
}

/** Debounced value, for search-as-you-type. */
export function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(value), ms);
    return () => window.clearTimeout(id);
  }, [value, ms]);
  return debounced;
}
