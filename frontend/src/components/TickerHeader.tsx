import { RefreshCw } from "lucide-react";
import type { TickerDetail } from "../api/types";
import { timestamp } from "./format";

const statusCopy: Record<TickerDetail["status"], string> = {
  ready: "Ready",
  refreshing: "Updating",
  ingesting: "Analyzing",
  failed: "Failed",
};

const statusClass: Record<TickerDetail["status"], string> = {
  ready: "bg-emerald-50 text-emerald-700 ring-emerald-100",
  refreshing: "bg-violet-50 text-violet-700 ring-violet-100",
  ingesting: "bg-violet-50 text-violet-700 ring-violet-100",
  failed: "bg-red-50 text-red-700 ring-red-100",
};

export function TickerHeader({ data, onRefresh, refreshing }: {
  data: TickerDetail;
  onRefresh: () => void;
  refreshing?: boolean;
}) {
  const { ticker } = data;
  const metadata = [ticker.exchange, ticker.sector, ticker.industry].filter(Boolean).join(" · ");
  return (
    <div className="border-b border-line px-5 pb-4 pt-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] font-bold uppercase tracking-[0.15em] text-mute">Asset research</p>
          <h1 className="mt-1 truncate text-xl font-semibold tracking-tight">{ticker.company_name ?? ticker.symbol}</h1>
          <p className="mt-0.5 text-sm text-mute"><span className="font-semibold text-ink">{ticker.symbol}</span>{metadata ? ` · ${metadata}` : ""}</p>
        </div>
        <button onClick={onRefresh} disabled={refreshing} className="grid size-9 shrink-0 place-items-center rounded-lg border border-line bg-white text-mute hover:text-ink disabled:opacity-50" aria-label="Refresh stored research" title="Refresh stored research">
          <RefreshCw aria-hidden="true" size={16} className={refreshing ? "status-pulse" : ""} />
        </button>
      </div>
      <div className="mt-4 flex items-center justify-between gap-2">
        <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ring-1 ${statusClass[data.status]}`}>
          <span className={`size-1.5 rounded-full ${data.status === "failed" ? "bg-red-500" : data.status === "ready" ? "bg-emerald-500" : "bg-accent status-pulse"}`} />
          {statusCopy[data.status]}
        </span>
        <time className="text-right text-[11px] text-mute" title={data.last_ingested_at ?? undefined}>Stored {timestamp(data.last_ingested_at)}</time>
      </div>
    </div>
  );
}
