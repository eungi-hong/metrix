import { useSearchParams } from "react-router-dom";
import { describeError } from "../../api/errors";
import { usePulse } from "../../api/queries";
import { Empty, Notice, Panel, SectionTitle, cx } from "../../design/ui";
import { longDate } from "../../lib/format";
import { PulseList, PulseSkeleton, SectorStrip } from "./PulseList";

const RANGES = [1, 2, 5] as const;

export default function PulsePage() {
  const [params, setParams] = useSearchParams();
  const sessions = Number(params.get("sessions")) || 1;
  const pulse = usePulse(sessions);
  const covered = pulse.data?.sessions ?? [];
  return (
    <div className="grid gap-6">
      <header>
        <h1 className="text-title text-ink">Market pulse</h1>
        <p className="mt-1 text-body text-mute">
          The largest moves against each stock's own market and sector,{" "}
          {covered.length ? `${covered.length === 1 ? "on" : "from"} ${longDate(covered[0])}${covered.length > 1 ? ` to ${longDate(covered[covered.length - 1])}` : ""}` : "in the latest session"}.
        </p>
        <div className="mt-3 inline-flex rounded-control border border-line p-0.5" role="radiogroup" aria-label="Sessions">
          {RANGES.map((n) => (
            <button key={n} role="radio" aria-checked={sessions === n} onClick={() => setParams({ sessions: String(n) })}
              className={cx("h-7 rounded-[5px] px-3 text-body", sessions === n ? "bg-accent text-accent-contrast" : "text-mute hover:text-ink")}>
              {n === 1 ? "Latest session" : `${n} sessions`}
            </button>
          ))}
        </div>
      </header>
      {pulse.error ? <Notice problem={describeError(pulse.error)} onRetry={() => void pulse.refetch()} /> : null}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Panel className="p-4">
          <SectionTitle aside="Largest |SAR| first">Moves</SectionTitle>
          {pulse.isPending ? <PulseSkeleton rows={10} /> : pulse.data?.movers.length ? <PulseList movers={pulse.data.movers} showDate={sessions > 1} /> : <Empty title="No flagged moves in these sessions" />}
        </Panel>
        <Panel className="p-4">
          <SectionTitle aside={sessions === 1 ? "Latest session" : `${sessions} sessions`}>Sectors</SectionTitle>
          {pulse.data ? <SectorStrip sectors={pulse.data.sectors} /> : <PulseSkeleton rows={8} />}
        </Panel>
      </div>
    </div>
  );
}
