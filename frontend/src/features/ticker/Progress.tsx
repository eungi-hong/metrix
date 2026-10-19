/** What a running ingestion has done so far, from its live progress. */
import type { Job } from "../../api/types";
import { cx } from "../../design/ui";

type Step = { label: string; state: "done" | "doing" | "todo" };

export function steps(progress: Record<string, unknown> | null | undefined, status: string | undefined): Step[] {
  const stage = String(progress?.stage ?? (status === "queued" ? "queued" : "prices"));
  const sessions = Number(progress?.sessions ?? NaN);
  const movements = Number(progress?.movements ?? NaN);
  const done = Number(progress?.done ?? 0);
  const total = Number(progress?.total ?? 0);
  const fetched = stage === "detected" || stage === "enriching";
  const finished = status === "succeeded";
  return [
    { label: stage === "queued" ? "Waiting for a free research slot" : "Queued", state: stage === "queued" ? "doing" : "done" },
    { label: fetched && Number.isFinite(sessions) ? `Fetched ${sessions} sessions of prices` : "Fetching prices", state: fetched || finished ? "done" : stage === "prices" ? "doing" : "todo" },
    { label: Number.isFinite(movements) ? `Found ${movements} unusual ${movements === 1 ? "move" : "moves"}` : "Finding unusual moves", state: fetched || finished ? "done" : "todo" },
    { label: total ? `Explaining ${done} of ${total} recent moves` : "Explaining recent moves", state: finished ? "done" : stage === "enriching" ? "doing" : "todo" },
    { label: "Ready", state: finished ? "done" : "todo" },
  ];
}

export function Progress({ symbol, job }: { symbol: string; job: Pick<Job, "status" | "progress"> | null }) {
  const list = steps(job?.progress, job?.status);
  const total = Number(job?.progress?.total ?? 0);
  const done = Number(job?.progress?.done ?? 0);
  return (
    <section aria-live="polite" aria-label={`Researching ${symbol}`} className="rounded-card border border-line bg-surface p-4">
      <p className="text-body font-semibold text-ink">Researching {symbol}. This usually takes about a minute.</p>
      <ol className="mt-3 grid gap-1.5">
        {list.map((s) => (
          <li key={s.label} className={cx("flex items-center gap-2 text-body", s.state === "todo" ? "text-mute" : "text-ink")}>
            <span aria-hidden className={cx("inline-grid size-4 place-items-center rounded-full border text-[10px]", s.state === "done" ? "border-ink bg-ink text-accent-contrast" : s.state === "doing" ? "border-ink" : "border-line")}>
              {s.state === "done" ? "✓" : ""}
            </span>
            <span>{s.label}</span>
            <span className="sr-only">{s.state === "done" ? "(done)" : s.state === "doing" ? "(in progress)" : ""}</span>
          </li>
        ))}
      </ol>
      {total > 0 && (
        <div className="mt-3 h-1 overflow-hidden rounded-full bg-surface-2" aria-hidden>
          <div className="h-full bg-ink transition-[width]" style={{ width: `${Math.round((done / total) * 100)}%` }} />
        </div>
      )}
    </section>
  );
}
