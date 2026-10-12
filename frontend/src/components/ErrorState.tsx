import { AlertCircle, RotateCw } from "lucide-react";
import type { ApiError } from "../api/client";

export function ErrorState({ error, onRetry }: { error: ApiError | Error; onRetry: () => void }) {
  const api = error as ApiError;
  const title = api.kind === "credentials" ? "Check your provider keys" : api.kind === "quota" ? "Rate limit reached" : api.kind === "spend-cap" ? "Daily spend cap reached" : api.kind === "network" ? "Live API unavailable" : "Research is temporarily unavailable";
  const retry = api.retryAfter ? ` Try again in about ${api.retryAfter} seconds.` : "";
  return <div role="alert" className="m-5 rounded-xl border border-negative/25 bg-negative-soft p-4"><AlertCircle aria-hidden="true" size={18} className="text-negative" /><p className="mt-2 text-sm font-semibold text-ink">{title}</p><p className="mt-1 text-xs leading-5 text-negative">{error.message}{retry}</p><button onClick={onRetry} className="mt-3 inline-flex items-center gap-1 rounded-lg border border-negative/25 bg-panel px-2.5 py-1.5 text-xs font-semibold text-negative transition-colors hover:bg-negative-soft"><RotateCw size={13} /> Retry</button></div>;
}
