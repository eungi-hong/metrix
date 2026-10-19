/**
 * The ticker's moves: dense, sortable, filterable, URL-synced. j and k move
 * the selection, Enter opens the case file. On phones the rows become
 * two-line cards.
 */
import { useEffect, useRef } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import type { Movement } from "../../api/types";
import { Change, DriverChip, Empty, cx } from "../../design/ui";
import { plain, pp, shortDate, signedNumber, times } from "../../lib/format";
import { evidenceWord } from "../pulse/evidence";
import { SORTS, SORT_LABEL, applyFilters, readFilters, summary, topLink, type Sort } from "./table";

const OPTIONS: { name: string; label: string; choices: [string, string][] }[] = [
  { name: "direction", label: "Direction", choices: [["", "Any"], ["up", "Up"], ["down", "Down"]] },
  { name: "trigger", label: "Flagged by", choices: [["", "Any test"], ["raw", "Size"], ["abnormal", "Abnormal return"], ["both", "Both"]] },
  { name: "dominant", label: "Driver", choices: [["", "Any"], ["market", "Market"], ["sector", "Sector"], ["company", "Company"]] },
  { name: "evidence", label: "Evidence", choices: [["", "Any"], ["explained", "Explained"], ["pending", "Not yet"]] },
];

export function MovementsTable({ symbol, movements, selected, onSelect }: {
  symbol: string; movements: Movement[]; selected: string | null; onSelect: (date: string) => void;
}) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const filters = readFilters(params);
  const rows = applyFilters(movements, filters);
  const body = useRef<HTMLTableSectionElement>(null);

  const set = (name: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next, { replace: true });
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || e.metaKey || e.ctrlKey || e.altKey) return;
      const i = rows.findIndex((m) => m.date === selected);
      if (e.key === "j" || e.key === "k") {
        const next = rows[Math.min(Math.max(i + (e.key === "j" ? 1 : -1), 0), rows.length - 1)];
        if (next) {
          onSelect(next.date);
          body.current?.querySelector(`[data-date="${next.date}"]`)?.scrollIntoView({ block: "nearest" });
        }
      } else if (e.key === "Enter" && selected && (el === document.body || el.closest("[data-movements]"))) {
        navigate(`/t/${symbol}/m/${selected}`);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rows, selected, onSelect, navigate, symbol]);

  return (
    <section aria-labelledby="moves-title" data-movements>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 id="moves-title" className="mr-2 text-body font-semibold text-ink">Moves</h2>
        {OPTIONS.map((o) => (
          <label key={o.name} className={cx("inline-flex h-8 items-center gap-1 rounded-full border px-3 text-caption", params.get(o.name) ? "border-ink bg-surface-2 text-ink" : "border-line bg-surface text-mute")}>
            {o.label}
            <select value={params.get(o.name) ?? ""} onChange={(e) => set(o.name, e.target.value)} className="bg-transparent font-medium text-ink outline-none">
              {o.choices.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select>
          </label>
        ))}
        <label className={cx("inline-flex h-8 items-center gap-1 rounded-full border px-3 text-caption", filters.minSar ? "border-ink bg-surface-2 text-ink" : "border-line bg-surface text-mute")}>
          |SAR| at least
          <input type="number" min={0} step={0.5} inputMode="decimal" value={params.get("min_sar") ?? ""} placeholder="any"
            onChange={(e) => set("min_sar", e.target.value)} className="num w-12 bg-transparent font-medium text-ink outline-none" />
        </label>
        <label className="ml-auto inline-flex h-8 items-center gap-1 text-caption text-mute">
          Sort
          <select value={filters.sort} onChange={(e) => set("sort", e.target.value === "date" ? "" : e.target.value)} className="bg-transparent font-medium text-ink outline-none">
            {SORTS.map((s) => <option key={s} value={s}>{SORT_LABEL[s]}</option>)}
          </select>
        </label>
      </div>
      <p className="mb-2 text-caption text-mute" aria-live="polite">{summary(rows)}</p>

      {rows.length === 0 ? (
        <Empty title="No moves match these filters">
          <button className="underline" onClick={() => setParams(new URLSearchParams(), { replace: true })}>Clear the filters</button>
        </Empty>
      ) : (
        <>
          <div className="hidden overflow-x-auto rounded-card border border-line bg-surface md:block">
            <table className="w-full border-collapse text-table">
              <thead className="bg-surface text-caption text-mute">
                <tr className="border-b border-line">
                  <Th>Date</Th><Th right sort="abs_return" current={filters.sort}>Return</Th><Th right>Abnormal</Th>
                  <Th right sort="abs_sar" current={filters.sort}>SAR</Th><Th right sort="rvol" current={filters.sort} className="hidden lg:table-cell">RVOL</Th>
                  <Th>Driver</Th><Th>Top explanation</Th><Th right className="hidden lg:table-cell">Score</Th><Th right>Evidence</Th>
                </tr>
              </thead>
              <tbody ref={body}>
                {rows.map((m) => {
                  const top = topLink(m);
                  const active = m.date === selected;
                  return (
                    <tr key={m.date} data-date={m.date} aria-selected={active} onClick={() => onSelect(m.date)}
                      className={cx("h-9 cursor-pointer border-b border-line last:border-0", active ? "bg-surface-2" : "hover:bg-surface-2/60")}>
                      <td className="whitespace-nowrap px-3"><Link to={`/t/${symbol}/m/${m.date}`} className="num hover:underline" onClick={(e) => e.stopPropagation()}>{shortDate(m.date)}{m.date.slice(0, 4) !== new Date().toISOString().slice(0, 4) ? `, ${m.date.slice(0, 4)}` : ""}</Link></td>
                      <td className="px-3 text-right font-medium"><Change value={m.daily_return} /></td>
                      <td className="num px-3 text-right text-ink">{pp(m.decomposition?.abnormal)}</td>
                      <td className="num px-3 text-right">{signedNumber(m.decomposition?.sar)}</td>
                      <td className="num hidden px-3 text-right lg:table-cell">{times(m.volume_analysis?.relative_volume)}</td>
                      <td className="px-3"><DriverChip driver={m.decomposition?.dominant} compact /></td>
                      <td className="max-w-[340px] truncate px-3 text-ink">{top ? top.article.title ?? top.article.url : <span className="text-mute">{evidenceWord(m.evidence?.state)}</span>}</td>
                      <td className="num hidden px-3 text-right lg:table-cell">{top ? plain(top.relevance_score, 2) : ""}</td>
                      <td className="num px-3 text-right text-mute">{m.news.length + m.filings.length}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <ol className="grid gap-2 md:hidden">
            {rows.map((m) => {
              const top = topLink(m);
              return (
                <li key={m.date}>
                  <Link to={`/t/${symbol}/m/${m.date}`} className="block rounded-card border border-line bg-surface p-3">
                    <span className="flex items-center justify-between gap-2">
                      <span className="num text-body font-medium">{shortDate(m.date)}, {m.date.slice(0, 4)}</span>
                      <Change value={m.daily_return} className="text-body font-medium" />
                    </span>
                    <span className="mt-1 flex items-center gap-2 text-caption">
                      <DriverChip driver={m.decomposition?.dominant} compact />
                      <span className="truncate text-mute">{top ? top.article.title : evidenceWord(m.evidence?.state)}</span>
                    </span>
                  </Link>
                </li>
              );
            })}
          </ol>
        </>
      )}
    </section>
  );
}

function Th({ children, right, sort, current, className }: { children: React.ReactNode; right?: boolean; sort?: Sort; current?: Sort; className?: string }) {
  const [params, setParams] = useSearchParams();
  const label = <span className={cx("font-medium", sort && current === sort && "text-ink")}>{children}</span>;
  return (
    <th scope="col" aria-sort={sort && current === sort ? "descending" : undefined} className={cx("h-9 px-3 font-normal", right ? "text-right" : "text-left", className)}>
      {sort ? (
        <button className="hover:text-ink" onClick={() => { const next = new URLSearchParams(params); next.set("sort", sort); setParams(next, { replace: true }); }}>{label}</button>
      ) : label}
    </th>
  );
}
