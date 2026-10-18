/**
 * The breakdown of a move in percentage points: a zero-centred waterfall.
 * Each part starts where the previous one ended; the last ends at the actual
 * return. Direction is shown by position and the signed number, never by
 * colour alone.
 */
import type { Decomposition as D } from "../../api/types";
import { axisExtent, axisPosition, waterfall, type Part } from "../../lib/decomposition";
import { componentLabel, pp } from "../../lib/format";
import { cx } from "../../design/ui";

const FILL: Record<Part, string> = { baseline: "bg-line-strong/50", market: "bg-market", sector: "bg-sector", company: "bg-company" };
const LABEL: Record<Part, string> = { baseline: "Baseline", market: "Market", sector: "Sector", company: "Company" };

export function DecompositionBar({ decomposition, total, warning, compact = false }: {
  decomposition: D | null | undefined; total: number; warning?: string | null; compact?: boolean;
}) {
  if (!decomposition) {
    return (
      <p className="rounded-control bg-surface-2 px-3 py-2 text-body text-mute">
        Not decomposed{warning ? `: ${warning.replace(/^[a-z]/, (c) => c.toLowerCase())}` : "."}
      </p>
    );
  }
  const w = waterfall({ alpha: decomposition.alpha, market: decomposition.market, sector: decomposition.sector, abnormal: decomposition.abnormal });
  const extent = axisExtent(w);
  const rows = w.segments.filter((s) => !compact || s.part !== "baseline");
  const totalAt = axisPosition(total, extent);
  return (
    <figure className="m-0">
      <div className="relative" role="img" aria-label={sentence(decomposition, total)}>
        <div aria-hidden className="absolute bottom-0 top-0 w-px bg-line-strong/60" style={{ left: "50%" }} />
        {rows.map((segment) => {
          const left = axisPosition(Math.min(segment.start, segment.end), extent);
          const right = axisPosition(Math.max(segment.start, segment.end), extent);
          return (
            <div key={segment.part} className={cx("relative grid items-center", compact ? "h-6 grid-cols-[64px_1fr_72px]" : "h-8 grid-cols-[84px_1fr_84px]")}>
              <span className="text-caption text-mute">{LABEL[segment.part]}</span>
              <div className="relative h-full">
                <div className={cx("absolute top-1/2 h-3 -translate-y-1/2 rounded-[2px]", FILL[segment.part])}
                  style={{ left: `${left}%`, width: `${Math.max(right - left, 0.6)}%` }} />
              </div>
              <span className="num text-right text-table text-ink">{pp(segment.value)}</span>
            </div>
          );
        })}
        <div className={cx("relative grid items-center border-t border-line", compact ? "h-6 grid-cols-[64px_1fr_72px]" : "h-8 grid-cols-[84px_1fr_84px]")}>
          <span className="text-caption font-semibold text-ink">Total</span>
          <div className="relative h-full">
            <div aria-hidden className="absolute top-1 bottom-1 w-0.5 -translate-x-1/2 bg-ink" style={{ left: `${totalAt}%` }} />
          </div>
          <span className="num text-right text-table font-semibold text-ink">{pp(total)}</span>
        </div>
      </div>
      {!compact && <figcaption className="mt-3 text-body text-ink">{sentence(decomposition, total)}</figcaption>}
    </figure>
  );
}

export function sentence(d: D, total: number): string {
  const parts = { market: d.market, sector: d.sector, company: d.abnormal } as Record<string, number | null | undefined>;
  const lead = (d.dominant ?? "company") as "market" | "sector" | "company";
  const others = (["market", "sector", "company"] as const).filter((p) => p !== lead).map((p) => `${componentLabel[p].toLowerCase()} ${pp(parts[p])}`);
  const name = lead === "company" ? "Company-specific" : lead === "market" ? "Market-wide" : "Sector-wide";
  return `${name}: ${pp(parts[lead]).replace(" pp", "")} of ${pp(total)}. ${others[0][0].toUpperCase()}${others[0].slice(1)}, ${others[1]}.`;
}
