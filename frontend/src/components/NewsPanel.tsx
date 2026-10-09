import { ExternalLink, FileSearch, Tag } from "lucide-react";
import type { Movement } from "../api/types";
import { shortDate, tierLabel } from "./format";

const tierStyle: Record<string, string> = { easy: "bg-violet-50 text-violet-700", medium: "bg-sky-50 text-sky-700", hard: "bg-amber-50 text-amber-800" };

export function NewsPanel({ movement }: { movement: Movement | null }) {
  if (!movement) return <section id="news-panel" role="tabpanel" aria-labelledby="news-tab" className="px-5 py-8 text-center"><FileSearch className="mx-auto text-mute" size={26} /><p className="mt-3 text-sm font-semibold">Select a movement</p><p className="mt-1 text-xs leading-5 text-mute">Its linked evidence will be shown here.</p></section>;
  if (!movement.news.length) return <section id="news-panel" role="tabpanel" aria-labelledby="news-tab" className="px-5 py-8 text-center"><FileSearch className="mx-auto text-mute" size={26} /><p className="mt-3 text-sm font-semibold">Evidence is not linked yet</p><p className="mt-1 text-xs leading-5 text-mute">This movement is marked {movement.news_status}. Return after enrichment completes.</p></section>;
  return <section id="news-panel" role="tabpanel" aria-labelledby="news-tab" className="scrollbar-thin max-h-[calc(100vh-260px)] space-y-3 overflow-y-auto px-4 py-4 lg:min-h-[520px]">
    <div className="px-1"><p className="text-sm font-semibold">Evidence for {shortDate(movement.date)}</p><p className="mt-0.5 text-xs text-mute">Source reporting linked to this stored price movement.</p></div>
    {movement.news.map((linked) => <article key={linked.article.id} className="rounded-xl border border-line bg-white p-3">
      <div className="flex items-center justify-between gap-2"><span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold ${tierStyle[linked.relevance_tier]}`}><Tag size={10} />{tierLabel[linked.relevance_tier]}</span><span className="text-[11px] text-mute">Score {linked.relevance_score.toFixed(2)}</span></div>
      <h2 className="mt-2 text-sm font-semibold leading-5">{linked.article.title ?? "Untitled article"}</h2>
      <p className="mt-1 text-[11px] text-mute">{linked.article.source ?? "Unknown source"} · {shortDate(linked.article.published_at)}</p>
      {linked.rationale && <p className="mt-2 text-xs leading-5 text-mute">{linked.rationale}</p>}
      <a href={linked.article.url} target="_blank" rel="noreferrer" className="mt-3 inline-flex items-center gap-1 text-xs font-semibold text-accent hover:underline">Open source <ExternalLink size={12} /></a>
    </article>)}
  </section>;
}
