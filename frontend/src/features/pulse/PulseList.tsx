import { Link } from "react-router-dom";
import type { PulseMover, SectorReturn } from "../../api/types";
import { Change, DriverChip, Skeleton, cx } from "../../design/ui";
import { pp, shortDate, signedNumber } from "../../lib/format";
import { evidenceDetail, isPending } from "./evidence";

export function PulseList({ movers, limit, showDate = false }: { movers: PulseMover[]; limit?: number; showDate?: boolean }) {
  const rows = limit ? movers.slice(0, limit) : movers;
  return (
    <ol className="divide-y divide-line">
      {rows.map((m) => (
        <li key={`${m.symbol}-${m.date}`}>
          <Link to={`/t/${m.symbol}/m/${m.date}`} className="group block px-1 py-2.5 hover:bg-surface-2">
            <span className="grid grid-cols-[52px_auto_1fr_auto] items-baseline gap-x-3">
              <span className="mono text-body font-semibold text-ink">{m.symbol}</span>
              <Change value={m.daily_return} className="text-body font-medium" />
              <span className="num truncate text-caption text-mute" title="Standardised abnormal return">
                {m.sar != null ? `SAR ${signedNumber(m.sar)}` : ""}{showDate ? `${m.sar != null ? ", " : ""}${shortDate(m.date)}` : ""}
              </span>
              <DriverChip driver={m.dominant} compact />
            </span>
            <span className={cx("mt-0.5 line-clamp-2 block pl-[64px] text-caption", isPending(m.evidence) ? "text-mute" : "text-ink")}>
              {m.evidence === "explained" ? m.explanation : evidenceDetail(m.evidence, m.explanation)}
            </span>
            {m.abnormal_return != null && <span className="sr-only">Abnormal return {pp(m.abnormal_return)}</span>}
          </Link>
        </li>
      ))}
    </ol>
  );
}

export function PulseSkeleton({ rows = 6 }: { rows?: number }) {
  return (
    <div aria-hidden className="divide-y divide-line">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex h-[52px] items-center gap-3 py-2.5"><Skeleton className="h-4 w-12" /><Skeleton className="h-4 w-16" /><Skeleton className="h-3 flex-1" /></div>
      ))}
    </div>
  );
}

/** Sector benchmarks as zero-centred bars with signed values. */
export function SectorStrip({ sectors }: { sectors: SectorReturn[] }) {
  const values = sectors.map((s) => Math.abs(s.total_return ?? 0));
  const extent = Math.max(...values, 0.005) * 1.1;
  return (
    <ul className="grid gap-1">
      {sectors.map((s) => {
        const value = s.total_return;
        const width = value == null ? 0 : (Math.abs(value) / extent) * 50;
        return (
          <li key={s.symbol} className="grid h-6 grid-cols-[minmax(0,140px)_1fr_64px] items-center gap-2">
            <span className="truncate text-caption text-ink">{s.sector} <span className="mono text-mute">{s.symbol}</span></span>
            <span className="relative h-2.5">
              <span aria-hidden className="absolute bottom-[-3px] top-[-3px] w-px bg-line-strong/50" style={{ left: "50%" }} />
              {value != null && (
                <span aria-hidden className={cx("absolute top-0 h-full rounded-[2px]", value >= 0 ? "bg-up/70" : "bg-down/70")}
                  style={value >= 0 ? { left: "50%", width: `${width}%` } : { right: "50%", width: `${width}%` }} />
              )}
            </span>
            {value == null ? <span className="text-right text-caption text-mute">no data</span> : <Change value={value} className="text-right text-caption" />}
          </li>
        );
      })}
    </ul>
  );
}
