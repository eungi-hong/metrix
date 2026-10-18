import type { Movement } from "../../api/types";
import { cx } from "../../design/ui";
import { etTime, pct, shortDate } from "../../lib/format";
import { BUCKETS, BUCKET_LABEL, CAN_EXPLAIN, WIDTH, lanes, position, type Bucket, type Frame, type TimelineItem } from "../../lib/timeline";

// Short names for the rail's narrow buckets; the vertical list uses the full ones.
const RAIL_LABEL: Record<Bucket, string> = { before_prior_close: "Prior day", overnight: "Overnight", intraday: "Session", after_close: "After close", later: "Later" };
const EXPLAINS: Record<string, string> = { Yes: "Can explain it", Weakly: "Weakly", No: "Cannot explain it" };

export function itemsFor(movement: Movement): TimelineItem[] {
  return [
    ...movement.filings.map((f) => ({ id: `F-${f.accession}`, kind: "filing" as const, label: `${f.form}${f.events[0] ? `, ${f.events[0]}` : ""}`, at: f.accepted_at ?? null, timing: (f.timing ?? null) as Bucket | null, precision: (f.timing_precision ?? null) as "time" | "day" | null })),
    ...movement.news.map((l) => ({ id: `A-${l.article.id}`, kind: "article" as const, label: l.article.title ?? l.article.url, at: l.article.published_at ?? null, timing: (l.timing ?? null) as Bucket | null, precision: (l.timing_precision ?? null) as "time" | "day" | null })),
  ];
}

export function SessionTimeline({ movement, frame }: { movement: Movement; frame: (Frame & { previous: string }) | null | undefined }) {
  const items = itemsFor(movement);
  const { timed, dateOnly } = lanes(items);
  const count = (b: Bucket) => timed.filter((i) => i.timing === b).length;
  const leg = (b: Bucket) => b === "overnight" && movement.legs?.gap != null ? `Gap ${pct(movement.legs.gap)}` : b === "intraday" && movement.legs?.intraday != null ? `Session ${pct(movement.legs.intraday)}` : null;
  return (
    <div>
      {/* Wide screens: one horizontal rail. */}
      {frame && (
        <div className="hidden md:block">
          <div className="flex text-caption">
            {BUCKETS.map((b) => (
              <div key={b} style={{ width: `${WIDTH[b]}%` }} className="border-l border-line px-1.5 first:border-l-0">
                <p className="font-medium text-ink">{RAIL_LABEL[b]} <span className="num font-normal text-mute">{count(b)}</span></p>
                <p className="text-mute">{EXPLAINS[CAN_EXPLAIN[b]]}</p>
              </div>
            ))}
          </div>
          <div className="relative mt-2 h-20 rounded-control bg-surface-2">
            {BUCKETS.slice(1).map((b) => {
              const left = BUCKETS.slice(0, BUCKETS.indexOf(b)).reduce((s, x) => s + WIDTH[x], 0);
              return <div key={b} aria-hidden className="absolute bottom-0 top-0 w-px bg-line" style={{ left: `${left}%` }} />;
            })}
            {BUCKETS.map((b) => leg(b) && (
              <span key={b} className="num absolute top-1 text-caption text-mute" style={{ left: `${BUCKETS.slice(0, BUCKETS.indexOf(b)).reduce((s, x) => s + WIDTH[x], 0) + 1}%` }}>{leg(b)}</span>
            ))}
            {timed.map((item, i) => (
              <a key={item.id} href={`#${item.id}`} title={`${item.label}, ${etTime(item.at)}`}
                className={cx("absolute grid size-6 -translate-x-1/2 place-items-center rounded-full text-[11px] font-semibold",
                  item.kind === "filing" ? "bg-ink text-accent-contrast" : "border border-ink bg-surface text-ink")}
                style={{ left: `${position(item.at!, item.timing!, frame)}%`, bottom: `${6 + (i % 2) * 28}px` }}>
                <span aria-hidden>{item.kind === "filing" ? "F" : "A"}</span>
                <span className="sr-only">{item.label}, {etTime(item.at)}</span>
              </a>
            ))}
          </div>
          <div className="mt-1 flex text-caption text-mute">
            <span style={{ width: "15%" }} className="text-right">Prior close {etTime(frame.prior_close)}, {shortDate(frame.previous)}</span>
            <span style={{ width: "25%" }} className="text-right">Open {etTime(frame.open)}</span>
            <span style={{ width: "35%" }} className="text-right">Close {etTime(frame.close)}</span>
            <span style={{ width: "15%" }} className="text-right">Next open</span>
          </div>
        </div>
      )}
      {/* Phones, and no calendar frame: a vertical rail by bucket. */}
      <ol className={cx("grid gap-3", frame && "md:hidden")}>
        {BUCKETS.map((b) => {
          const here = timed.filter((i) => i.timing === b);
          return (
            <li key={b} className="border-l-2 border-line pl-3">
              <p className="text-caption font-medium text-ink">{BUCKET_LABEL[b]} <span className="font-normal text-mute">Can explain: {CAN_EXPLAIN[b].toLowerCase()}{leg(b) ? `. ${leg(b)}` : ""}</span></p>
              {here.length ? (
                <ul className="mt-1 grid gap-1">{here.map((i) => <li key={i.id} className="text-body"><a href={`#${i.id}`} className="hover:underline">{i.label}</a> <span className="text-caption text-mute">{etTime(i.at)}</span></li>)}</ul>
              ) : <p className="text-caption text-mute">Nothing</p>}
            </li>
          );
        })}
      </ol>
      {dateOnly.length > 0 && (
        <div className="mt-3">
          <p className="text-caption font-medium text-ink">Known only by date</p>
          <ul className="mt-1 grid gap-1">{dateOnly.map((i) => <li key={i.id} className="text-body"><a href={`#${i.id}`} className="hover:underline">{i.label}</a> <span className="text-caption text-mute">{i.at ? shortDate(i.at) : "date unknown"}</span></li>)}</ul>
        </div>
      )}
    </div>
  );
}
