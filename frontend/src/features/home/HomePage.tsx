/**
 * The first ten seconds: real, pre-warmed data with no clicks and no key
 * prompt. The featured case file is the API's pick: a large recent move whose
 * evidence clearly explains it. The pulse lists explained moves first, so a
 * visitor's first look is at answers, not at moves still waiting for news.
 */
import { Link } from "react-router-dom";
import { describeError } from "../../api/errors";
import { useMovementCase, usePulse } from "../../api/queries";
import type { PulseMover } from "../../api/types";
import { DriverChip, Empty, Notice, Panel, SectionTitle, Skeleton, buttonClass } from "../../design/ui";
import { longDate, pct, shortDate, timingLabel } from "../../lib/format";
import { DecompositionBar } from "../movement/Decomposition";
import { caseTitle } from "../movement/title";
import { PulseList, PulseSkeleton, SectorStrip } from "../pulse/PulseList";
import { useFallbackFeatured } from "./fallbackFeatured";

const PULSE_SESSIONS = 5;

export default function HomePage() {
  // A trading week, so two quiet days in a row never leave the page empty.
  const pulse = usePulse(PULSE_SESSIONS);
  const movers = explainedFirst(pulse.data?.movers ?? []);
  const fromPulse = pulse.data?.featured ?? movers.find((m) => m.evidence === "explained");
  const fallback = useFallbackFeatured(Boolean(pulse.data) && !fromPulse);
  const featured = fromPulse ?? fallback ?? undefined;
  const through = pulse.data?.sessions.at(-1);
  return (
    <div className="grid gap-6">
      <header>
        <h1 className="max-w-[28ch] text-display-sm text-ink md:text-display">Every unusual daily move, explained with cited news and SEC filings.</h1>
        <p className="mt-2 text-body text-mute">{through ? `Data through the close on ${longDate(through)}, 4:00 pm ET.` : "Loading the latest session."}</p>
      </header>
      {pulse.error ? <Notice problem={describeError(pulse.error)} onRetry={() => void pulse.refetch()} /> : null}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <Panel className="p-5" aria-labelledby="featured">
          <SectionTitle id="featured">Featured case file</SectionTitle>
          {featured ? <Featured mover={featured} /> : pulse.isPending ? <FeaturedSkeleton /> : <Empty title="No explained moves yet">Search for a ticker to research it.</Empty>}
        </Panel>
        <div className="grid content-start gap-6">
          <Panel className="p-4" aria-labelledby="pulse">
            <SectionTitle id="pulse" aside={<Link className="underline underline-offset-2 hover:text-ink" to="/pulse">See all</Link>}>Market pulse</SectionTitle>
            {pulse.isPending ? <PulseSkeleton /> : movers.length ? <PulseList movers={movers} limit={8} showDate /> : <Empty title="No unusual moves in the last week" />}
          </Panel>
          <Panel className="p-4" aria-labelledby="sectors">
            <SectionTitle id="sectors" aside={`Last ${PULSE_SESSIONS} sessions`}>Sectors</SectionTitle>
            {pulse.data ? <SectorStrip sectors={pulse.data.sectors} /> : <PulseSkeleton rows={5} />}
          </Panel>
        </div>
      </div>
      {movers.length > 0 && <Suggestions movers={featured ? [featured, ...movers.filter((m) => m !== featured && !sameMove(m, featured))] : movers} />}
    </div>
  );
}

function Featured({ mover }: { mover: PulseMover }) {
  const c = useMovementCase(mover.symbol, mover.date);
  const movement = c.data?.movement;
  const evidence = movement?.news.slice(0, 2) ?? [];
  const filing = movement?.filings.find((f) => f.role === "primary") ?? movement?.filings[0];
  return (
    <article>
      <h3 className="text-heading text-ink">
        <Link to={`/t/${mover.symbol}/m/${mover.date}`} className="hover:underline">
          {caseTitle(mover.symbol, mover.daily_return, mover.date, mover.evidence === "explained")}
        </Link>
      </h3>
      <p className="mt-1 text-caption text-mute">{mover.company_name ?? mover.symbol} <DriverChip driver={mover.dominant} /></p>
      <div className="mt-4">
        {movement ? <DecompositionBar decomposition={movement.decomposition} total={movement.daily_return} warning={movement.decomposition_warning} compact /> : <Skeleton className="h-[120px] w-full" />}
      </div>
      <ol className="mt-4 grid gap-2">
        {filing && (
          <li className="grid grid-cols-[20px_1fr] gap-2 text-body">
            <span className="num text-mute">F</span>
            <span><span className="font-medium">{filing.form}{filing.events[0] ? `, ${filing.events[0]}` : ""}</span> <span className="text-mute">{filing.timing ? timingLabel[filing.timing] : ""}</span></span>
          </li>
        )}
        {evidence.map((link, i) => (
          <li key={link.article.id} className="grid grid-cols-[20px_1fr] gap-2 text-body">
            <span className="num text-mute">{i + 1}</span>
            <span><span className="font-medium text-ink">{link.article.title ?? link.article.url}</span>{" "}<span className="text-mute">{link.article.source}{link.timing ? `, ${timingLabel[link.timing].toLowerCase()}` : ""}</span></span>
          </li>
        ))}
        {!c.isPending && !evidence.length && !filing && <li className="text-body text-mute">{mover.explanation}</li>}
      </ol>
      <div className="mt-5 flex flex-wrap gap-2">
        <Link className={buttonClass("primary")} to={`/t/${mover.symbol}/m/${mover.date}`}>Open case file</Link>
        <Link className={buttonClass()} to={`/ask?q=${encodeURIComponent(`Why did ${mover.symbol} ${mover.daily_return < 0 ? "fall" : "rise"} on ${mover.date}?`)}&ticker=${mover.symbol}`}>
          Ask about this move
        </Link>
      </div>
    </article>
  );
}

/** Explained moves first (each group keeps the API's order), failed research last. */
export function explainedFirst(movers: PulseMover[]): PulseMover[] {
  const rank = (m: PulseMover) => (m.evidence === "explained" ? 0 : m.evidence === "failed" ? 2 : 1);
  return movers.map((m, i) => [m, i] as const).sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(([m]) => m);
}

function sameMove(a: PulseMover, b: PulseMover) {
  return a.symbol === b.symbol && a.date === b.date;
}

function FeaturedSkeleton() {
  return <div aria-hidden className="grid gap-3"><Skeleton className="h-7 w-3/4" /><Skeleton className="h-[120px] w-full" /><Skeleton className="h-4 w-2/3" /><Skeleton className="h-4 w-1/2" /></div>;
}

function Suggestions({ movers }: { movers: PulseMover[] }) {
  const questions = movers.slice(0, 3).map((m) => `Why did ${m.symbol} ${m.daily_return < 0 ? "fall" : "rise"} ${pct(Math.abs(m.daily_return)).replace("+", "")} on ${shortDate(m.date)}?`);
  return (
    <section aria-labelledby="try" className="flex flex-wrap items-center gap-2">
      <h2 id="try" className="text-body text-mute">Try asking</h2>
      {questions.map((q) => (
        <Link key={q} to={`/ask?q=${encodeURIComponent(q)}`} className="rounded-full border border-line bg-surface px-3 py-1 text-body text-ink hover:bg-surface-2">{q}</Link>
      ))}
    </section>
  );
}
