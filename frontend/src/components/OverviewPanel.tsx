import { AlertTriangle, ArrowDownRight, ArrowUpRight, BookOpenText, CalendarRange, Layers3 } from "lucide-react";
import type { TickerDetail } from "../api/types";
import { pct, shortDate, tierLabel } from "./format";

export function OverviewPanel({ data }: { data: TickerDetail }) {
  const citedArticles = data.movements.reduce((total, move) => total + move.news.length, 0);
  const recent = data.movements[0];
  const stats = [
    { icon: Layers3, label: "Detected", value: String(data.pagination.total), sub: "major moves" },
    { icon: CalendarRange, label: "Coverage", value: data.price_range.bars ? `${data.price_range.bars} bars` : "—", sub: data.price_range.start ? `${shortDate(data.price_range.start)} onward` : "not yet stored" },
    { icon: BookOpenText, label: "Cited", value: String(citedArticles), sub: "linked articles" },
  ];
  return (
    <section id="overview-panel" role="tabpanel" aria-labelledby="overview-tab" className="space-y-5 px-5 py-5">
      <div>
        <p className="text-sm font-semibold">Large price moves, explained by evidence.</p>
        <p className="mt-1 text-sm leading-5 text-mute">Metrix links unusual stored price moves with company, industry, and macro reporting. It does not make trading recommendations.</p>
      </div>

      {data.warnings.map((warning) => (
        <div key={warning} className="flex gap-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs leading-5 text-amber-900">
          <AlertTriangle aria-hidden="true" size={15} className="mt-0.5 shrink-0" />
          <p>{warning}</p>
        </div>
      ))}

      <dl className="grid grid-cols-3 gap-2">
        {stats.map(({ icon: Icon, label, value, sub }) => (
          <div key={label} className="rounded-xl border border-line bg-white p-2.5">
            <Icon aria-hidden="true" size={14} className="text-accent" />
            <dt className="mt-2 text-[10px] font-bold uppercase tracking-[0.12em] text-mute">{label}</dt>
            <dd className="mt-0.5 text-sm font-semibold leading-4">{value}</dd>
            <p className="mt-0.5 text-[10px] leading-3 text-mute">{sub}</p>
          </div>
        ))}
      </dl>

      <div className="rounded-xl border border-line bg-white p-3">
        <p className="text-[11px] font-bold uppercase tracking-[0.13em] text-mute">Most recent move</p>
        {recent ? (
          <div className="mt-2 flex items-center gap-2">
            <span className={`grid size-7 place-items-center rounded-full ${recent.direction === "up" ? "bg-emerald-50 text-emerald-700" : "bg-red-50 text-red-700"}`}>
              {recent.direction === "up" ? <ArrowUpRight size={16} /> : <ArrowDownRight size={16} />}
            </span>
            <div><p className="text-sm font-semibold">{shortDate(recent.date)} · <span className={recent.direction === "up" ? "text-emerald-700" : "text-red-700"}>{pct(recent.daily_return_pct, true)}</span></p><p className="text-xs text-mute">{recent.news_status === "complete" ? `${recent.news.length} linked evidence item${recent.news.length === 1 ? "" : "s"}` : `Evidence ${recent.news_status}`}</p></div>
          </div>
        ) : <p className="mt-2 text-sm text-mute">No qualifying movements in the stored range.</p>}
      </div>

      <div>
        <p className="text-[11px] font-bold uppercase tracking-[0.13em] text-mute">Evidence tiers</p>
        <div className="mt-2 space-y-2">
          {Object.entries(tierLabel).map(([tier, label]) => <div key={tier} className="flex items-center justify-between text-xs"><span className="font-semibold">{label}</span><span className="text-mute">{tier === "easy" ? "Company-specific" : tier === "medium" ? "Peers & industry" : "Macro & policy"}</span></div>)}
        </div>
      </div>
    </section>
  );
}
