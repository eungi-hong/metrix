import { AlertCircle, RotateCw } from "lucide-react";
import type { ApiError } from "../api/client";

export function ErrorState({ error, onRetry }: { error: ApiError | Error; onRetry: () => void }) {
  const api = error as ApiError;
  const title = api.kind === "credentials" ? "Check your API key" : api.kind === "quota" ? "Rate limit reached" : api.kind === "spend-cap" ? "Daily spend cap reached" : api.kind === "network" ? "Local API unavailable" : "Research is temporarily unavailable";
  const retry = api.retryAfter ? ` Try again in about ${api.retryAfter} seconds.` : "";
  return <div role="alert" className="m-5 rounded-xl border border-red-200 bg-red-50 p-4"><AlertCircle aria-hidden="true" size={18} className="text-red-700" /><p className="mt-2 text-sm font-semibold text-red-950">{title}</p><p className="mt-1 text-xs leading-5 text-red-800">{error.message}{retry}</p><button onClick={onRetry} className="mt-3 inline-flex items-center gap-1 rounded-lg border border-red-200 bg-white px-2.5 py-1.5 text-xs font-semibold text-red-800 hover:bg-red-100"><RotateCw size={13} /> Retry</button></div>;
}
