import type { Job, TickerDetail } from "../api/types";

export type ResearchPhase = "idle" | "loading" | "ready" | "processing" | "failed";

export function phaseForTicker(data: TickerDetail | null, loading: boolean): ResearchPhase {
  if (loading) return "loading";
  if (!data) return "idle";
  if (data.status === "failed") return "failed";
  if (data.status === "ingesting" || data.status === "refreshing") return "processing";
  return "ready";
}

export function shouldRefetchForJob(job: Job): boolean {
  return job.status === "succeeded";
}

export function jobFailureMessage(job: Job): string | null {
  return job.status === "dead"
    ? job.last_error ?? "Analysis could not finish. Try again when the upstream service recovers."
    : null;
}
