import { ArrowDownRight, ArrowUpRight, ChevronRight } from "lucide-react";
import type { Movement } from "../api/types";
import { pct, shortDate } from "./format";

export function MovementsPanel({ movements, selectedId, onSelect }: {
  movements: Movement[];
  selectedId: number | null;
  onSelect: (movement: Movement) => void;
}) {
  return (
    <section id="movements-panel" role="tabpanel" aria-labelledby="movements-tab" className="scrollbar-thin max-h-[calc(100vh-260px)] overflow-y-auto px-3 py-3 lg:min-h-[520px]">
      <p className="px-2 pb-2 text-xs leading-5 text-mute">A move is flagged when it clears the greater of the configured floor and volatility threshold.</p>
      {movements.length ? <div className="space-y-1.5">{movements.map((movement) => {
        const selected = movement.id === selectedId;
        const positive = movement.direction === "up";
        const rationale = movement.news[0]?.rationale;
        return <button key={movement.id} onClick={() => onSelect(movement)} className={`w-full rounded-xl border p-3 text-left transition-colors ${selected ? "border-violet-200 bg-violet-50/70" : "border-transparent hover:border-line hover:bg-white"}`}>
          <div className="flex items-center gap-2">
            <span className={`grid size-7 shrink-0 place-items-center rounded-full ${positive ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>{positive ? <ArrowUpRight size={16} /> : <ArrowDownRight size={16} />}</span>
            <div className="min-w-0 flex-1"><div className="flex items-baseline justify-between gap-2"><span className="text-sm font-semibold">{shortDate(movement.date)}</span><span className={`text-sm font-bold ${positive ? "text-emerald-700" : "text-red-700"}`}>{pct(movement.daily_return_pct, true)}</span></div><p className="mt-0.5 truncate text-xs text-mute">{movement.news_status === "complete" ? `${movement.news.length} evidence item${movement.news.length === 1 ? "" : "s"}` : `Evidence ${movement.news_status}`}</p></div>
            <ChevronRight aria-hidden="true" size={15} className="text-mute" />
          </div>
          <p className="mt-2 line-clamp-2 text-xs leading-4 text-mute"><span className="font-medium text-ink">{movement.sigma_multiple?.toFixed(1) ?? "—"}σ · {pct(movement.threshold * 100)}</span>{rationale ? ` — ${rationale}` : " — Evidence is still being collected."}</p>
        </button>;
      })}</div> : <div className="rounded-xl border border-dashed border-line bg-white p-6 text-center"><p className="text-sm font-semibold">No movements yet</p><p className="mt-1 text-xs leading-5 text-mute">When stored history is available, detected movements will appear here.</p></div>}
    </section>
  );
}
