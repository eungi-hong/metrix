/** /methodology: how Metrix decides what moved, why, and how well it does. */
import { useEffect } from "react";
import { Link } from "react-router-dom";
import { useMovementCase, usePulse } from "../../api/queries";
import { Panel, Skeleton } from "../../design/ui";
import { longDate, pct, plain, pp, signedNumber } from "../../lib/format";
import { BUCKETS, BUCKET_LABEL, CAN_EXPLAIN } from "../../lib/timeline";
import { loadResults, type ResultFile } from "./results";

const SECTIONS = [
  ["detect", "Detect"], ["decompose", "Decompose"], ["time", "Time"], ["evidence", "Find evidence"],
  ["score", "Score"], ["evaluate", "Evaluate"], ["limits", "Limits"],
] as const;

export default function MethodologyPage() {
  useEffect(() => { document.title = "How Metrix works | Metrix"; }, []);
  return (
    <div className="grid gap-8 lg:grid-cols-[200px_minmax(0,720px)]">
      <nav aria-label="On this page" className="hidden lg:block">
        <ol className="sticky top-20 grid gap-1 text-body">
          {SECTIONS.map(([id, label], i) => (
            <li key={id}><a href={`#${id}`} className="flex gap-2 text-mute hover:text-ink"><span className="num w-5">{String(i + 1).padStart(2, "0")}</span>{label}</a></li>
          ))}
        </ol>
      </nav>
      <article className="grid gap-10 text-body-lg text-ink">
        <header>
          <h1 className="text-title">How Metrix works</h1>
          <p className="mt-2 text-mute">A stock's daily move is flagged when it is unusual for that stock, split into the parts its market and its sector explain, and matched to news and SEC filings by when they were published and what they say. Every step below can be reproduced from the code and the committed results.</p>
        </header>

        <Section n={1} id="detect" title="Detect">
          <p>A day is a movement when its return is large for this stock. The size test compares the close-to-close return with a floor and with the stock's own recent volatility:</p>
          <Formula>|r| ≥ max(floor, k × σ<sub>20</sub>)</Formula>
          <p>The abnormal test asks the same of the part of the move the market and sector do not explain (below): |SAR| ≥ 2.5 and the move clears the floor. Metrix runs the size test by default; a movement records which test flagged it, and every case file shows both tests with their inputs.</p>
        </Section>

        <Section n={2} id="decompose" title="Decompose">
          <p>Each day's return is split into a market part (SPY), a sector part (the sector ETF, with the market taken out of it, so the two never double count) and what is left, the company's own move. The model is fitted by least squares on the 120 sessions before the move.</p>
          <Formula>r<sub>i,t</sub> = α + β<sub>m</sub> r<sub>m,t</sub> + β<sub>s</sub> s⊥<sub>t</sub> + e<sub>t</sub></Formula>
          <Formula>SAR = abnormal / (σ<sub>e</sub> √(1 + x′(X′X)⁻¹x))</Formula>
          <p>The standardised abnormal return (SAR) is a size in standard deviations, a heuristic, not a p-value. Earlier shocks in the window are clipped so one old earnings day does not inflate the scale.</p>
          <WorkedExample />
        </Section>

        <Section n={3} id="time" title="Time">
          <p>Every article and filing is placed in the trading session around the move, on the New York Stock Exchange calendar (early closes and holidays included). Only news before the close can have caused the close-to-close move.</p>
          <table className="mt-2 w-full text-table">
            <thead className="text-caption text-mute"><tr className="border-b border-line"><th scope="col" className="py-1 text-left font-normal">When</th><th scope="col" className="text-left font-normal">Can it explain the move?</th></tr></thead>
            <tbody>{BUCKETS.map((b) => <tr key={b} className="border-b border-line last:border-0"><td className="py-1.5">{BUCKET_LABEL[b]}</td><td>{CAN_EXPLAIN[b]}</td></tr>)}</tbody>
          </table>
          <p className="mt-2">An article known only by its date is never placed at an invented hour.</p>
        </Section>

        <Section n={4} id="evidence" title="Find evidence">
          <p>Three searches per move, around its session: news about the company, about its competitors and industry, and about the economy and policy. Where SEC EDGAR is enabled, the company's filings in the same window (an 8-K's items say what it reports, for example results of operations) are attached as primary sources.</p>
        </Section>

        <Section n={5} id="score" title="Score">
          <p>All candidates for one move go to a language model in a single call, with the move's size, direction and timing, and are scored for whether they explain it. Articles below 0.35 are not linked. Scorer v2, opt-in until it is shown to be better, also sees the breakdown and the session legs, can mark a link as the primary cause, a contributing factor or context, and can say that nothing explains the move. Anything published after the next session opened is never linked, whatever its score.</p>
        </Section>

        <Section n={6} id="evaluate" title="Evaluate">
          <Evaluation results={loadResults()} />
        </Section>

        <Section n={7} id="limits" title="Limits">
          <ul className="grid list-disc gap-1 pl-5">
            <li>The relevance scoring has not yet been measured against human labels (see above).</li>
            <li>The sector benchmark includes the stock itself, so a very large constituent leaks into its own sector factor.</li>
            <li>Article text is what the search provider returns, often a summary.</li>
            <li>Earnings come from SEC filings only: no estimates or surprises yet.</li>
            <li>Only unusually large days are stored, so this is not a full news archive.</li>
          </ul>
        </Section>
      </article>
    </div>
  );
}

function Section({ n, id, title, children }: { n: number; id: string; title: string; children: React.ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-h`} className="grid scroll-mt-20 gap-3">
      <h2 id={`${id}-h`} className="flex items-baseline gap-3 text-heading"><span className="num text-mute">{String(n).padStart(2, "0")}</span>{title}</h2>
      {children}
    </section>
  );
}

function Formula({ children }: { children: React.ReactNode }) {
  return <p className="mono overflow-x-auto rounded-control bg-surface-2 px-4 py-3 text-body">{children}</p>;
}

function WorkedExample() {
  const pulse = usePulse(5);
  const pick = pulse.data?.movers.find((m) => m.evidence === "explained" && m.abnormal_return != null) ?? pulse.data?.movers[0];
  const c = useMovementCase(pick?.symbol ?? "", pick?.date ?? "");
  if (!pick) return pulse.isPending ? <Skeleton className="h-28 w-full" /> : null;
  const d = c.data?.movement.decomposition;
  return (
    <Panel className="p-4 text-body">
      <p className="text-caption text-mute">Worked example, live from the API</p>
      <p className="mt-1 font-medium"><Link className="underline underline-offset-2" to={`/t/${pick.symbol}/m/${pick.date}`}>{pick.symbol} on {longDate(pick.date)}</Link>: {pct(pick.daily_return)}</p>
      {d ? (
        <p className="num mt-2">
          {pct(c.data!.movement.daily_return, 2)} = baseline {pp(d.alpha, 2)} + market {pp(d.market, 2)} (β {plain(d.beta_market, 2)} × {d.market_benchmark}) + sector {pp(d.sector, 2)} + company {pp(d.abnormal, 2)}. SAR {signedNumber(d.sar)}.
        </p>
      ) : c.isPending ? <Skeleton className="mt-2 h-5 w-full" /> : <p className="mt-2 text-mute">This move could not be decomposed.</p>}
    </Panel>
  );
}

const RELEVANCE_ROWS: [string, string][] = [["hit_at_1", "Top explanation correct (Hit@1)"], ["recall_at_5", "A correct one in the top 5"], ["tier_accuracy", "Tier correct"], ["timing_violations", "Links published too late (target 0)"]];
const CHAT_ROWS: [string, string][] = [["citation_precision", "Citations on the question's tickers"], ["required_recall", "Required sources cited"], ["uncited_numeric_claims", "Numbers without a citation"], ["mismatched_numbers", "Numbers that do not match the data"], ["no_data_questions_passed", "Unanswerable questions handled"]];

function Evaluation({ results }: { results: ResultFile[] }) {
  const relevance = results.filter((r) => r.kind === "relevance");
  const chat = results.filter((r) => r.kind === "chat");
  return (
    <div className="grid gap-5">
      <div>
        <h3 className="text-body font-semibold">Explanations</h3>
        {relevance.length ? relevance.map((r) => <ResultTable key={r.file} result={r} rows={RELEVANCE_ROWS} />) : (
          <p className="mt-1 text-body text-mute">Not measured yet. The harness, the metrics with 95% bootstrap intervals and the replay are built, but the human-labelled set is still empty, so no accuracy figure is claimed. Reproduce, once labelled: <code className="mono">python -m evals.run --replay</code>.</p>
        )}
      </div>
      <div>
        <h3 className="text-body font-semibold">Ask Metrix</h3>
        {chat.length ? chat.map((r) => <ResultTable key={r.file} result={r} rows={CHAT_ROWS} />) : <p className="mt-1 text-body text-mute">Not measured yet.</p>}
      </div>
    </div>
  );
}

function ResultTable({ result, rows }: { result: ResultFile; rows: [string, string][] }) {
  return (
    <div className="mt-2">
      <p className="text-caption text-mute">{result.name}{result.mode ? `, ${result.mode === "fallback" ? "database summary, no model" : result.mode}` : ""}{result.runAt ? `, ${longDate(result.runAt)}` : ""}. Synthetic local data.</p>
      <table className="mt-1 w-full text-table">
        <tbody>
          {rows.filter(([key]) => key in result.metrics).map(([key, label]) => (
            <tr key={key} className="border-b border-line last:border-0"><td className="py-1.5">{label}</td><td className="num text-right">{format(result.metrics[key])}</td></tr>
          ))}
        </tbody>
      </table>
      {result.command && <p className="mt-1 text-caption text-mute">Reproduce: <code className="mono">{result.command}</code> ({result.file})</p>}
    </div>
  );
}

function format(value: unknown): string {
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(3);
  return value == null ? "n/a" : String(value);
}
