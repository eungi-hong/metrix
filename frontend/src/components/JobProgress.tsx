import { LoaderCircle } from "lucide-react";
import type { Job, TickerDetail } from "../api/types";

export function JobProgress({ data, job }: { data: TickerDetail; job: Job | null }) {
  if (data.status !== "ingesting" && data.status !== "refreshing") return null;
  const progress = job?.progress;
  const done = typeof progress?.done === "number" ? progress.done : null;
  const total = typeof progress?.total === "number" ? progress.total : null;
  return <div className="mx-5 mt-4 flex gap-2 rounded-xl border border-accent-line bg-accent-soft px-3 py-2.5 text-xs leading-5 text-ink"><LoaderCircle size={16} className="status-pulse mt-0.5 shrink-0 text-accent" /><p><span className="font-semibold">Analysis in progress.</span> {data.message ?? "Stored data will refresh automatically."}{done !== null && total !== null ? ` ${done} of ${total} steps reported.` : ""}</p></div>;
}
