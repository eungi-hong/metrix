/**
 * /t/:symbol/m/:date: everything about one move, on one page worth sharing.
 * The title never overclaims: without an accepted explanation it says no
 * clear driver was found.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { describeError } from "../../api/errors";
import { useMovementCase } from "../../api/queries";
import type { LinkedArticle, MovementCase } from "../../api/types";
import { Button, Change, DriverChip, Kbd, Notice, Panel, Pill, SectionTitle, Skeleton, buttonClass, cx } from "../../design/ui";
import { etDateTime, longDate, pct, plain, pp, signedNumber, tierLabel, timingLabel, times } from "../../lib/format";
import { EvidenceLine } from "../ticker/EvidenceLine";
import { DecompositionBar } from "./Decomposition";
import { SessionTimeline } from "./SessionTimeline";
import { caseTitle } from "./title";

const ROLE: Record<string, string> = { primary: "Primary cause", contributing: "Contributing", context: "Context" };
const DRIFT: Record<string, string> = { held: "Held", continued: "Continued", reversed: "Reversed" };

export default function CasePage() {
  const params = useParams();
  const symbol = (params.symbol ?? "").toUpperCase();
  const day = params.date ?? "";
  const c = useMovementCase(symbol, day);
  const navigate = useNavigate();
  const [copied, setCopied] = useState(false);
  const askHref = `/ask?ticker=${symbol}&q=${encodeURIComponent(`Why did ${symbol} move on ${day}?`)}`;

  useEffect(() => {
    if (!c.data) return;
    const m = c.data.movement;
    const title = caseTitle(symbol, m.daily_return, day, m.evidence?.state === "explained");
    document.title = `${title} | Metrix`;
    setMeta("description", `${title}. ${m.news[0]?.rationale ?? m.evidence?.detail ?? ""}`.slice(0, 200));
  }, [c.data, day, symbol]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "a") navigate(askHref);
      if (e.key === "c") void copy();
      if (e.key === "Escape") navigate(`/t/${symbol}?m=${day}`);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch { /* clipboard blocked */ }
  };

  const crumbs = (
    <nav aria-label="Breadcrumb" className="text-caption text-mute">
      <Link to={`/t/${symbol}`} className="mono hover:text-ink">{symbol}</Link> / <Link to={`/t/${symbol}`} className="hover:text-ink">Moves</Link> / <span aria-current="page">{longDate(day)}</span>
    </nav>
  );

  if (c.isPending) return <div className="grid gap-4">{crumbs}<Skeleton className="h-10 w-3/4" label="Loading the case file" /><Skeleton className="h-6 w-1/2" /><Skeleton className="h-[220px] w-full" /><Skeleton className="h-[160px] w-full" /></div>;
  if (c.error || !c.data) return <div className="grid gap-4">{crumbs}<Notice problem={describeError(c.error)} onRetry={() => void c.refetch()} /></div>;

  const data = c.data;
  const m = data.movement;
  const explained = m.evidence?.state === "explained";
  const primaryFiling = m.filings.find((f) => f.role === "primary") ?? m.filings.find((f) => f.items.includes("2.02")) ?? null;

  return (
    <article className="grid gap-6">
      <header className="grid gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          {crumbs}
          <div className="flex gap-2">
            <Button onClick={() => void copy()} aria-live="polite">{copied ? "Link copied" : "Copy link"} <span className="hidden md:inline"><Kbd>c</Kbd></span></Button>
            <Link to={askHref} className={buttonClass("primary")}>Ask about this move <span className="hidden md:inline"><Kbd>a</Kbd></span></Link>
          </div>
        </div>
        <h1 className="max-w-[24ch] text-display-sm text-ink md:text-display">{caseTitle(symbol, m.daily_return, day, explained)}</h1>
        <p className="text-body text-mute">{data.ticker.company_name ?? symbol}{data.ticker.sector ? `, ${data.ticker.sector}` : ""}</p>
        <ul className="flex flex-wrap gap-2" aria-label="Key figures">
          <li><Pill>Return <Change value={m.daily_return} digits={2} /></Pill></li>
          {m.decomposition?.sar != null && <li><Pill>SAR <span className="num">{signedNumber(m.decomposition.sar)}</span></Pill></li>}
          {m.volume_analysis?.relative_volume != null && <li><Pill>Volume <span className="num">{times(m.volume_analysis.relative_volume)}</span> usual</Pill></li>}
          {m.legs?.gap != null && <li><Pill>Gap <span className="num">{pct(m.legs.gap)}</span></Pill></li>}
          {m.legs?.intraday != null && <li><Pill>Session <span className="num">{pct(m.legs.intraday)}</span></Pill></li>}
          <li><Pill>Flagged by {m.trigger === "both" ? "both tests" : m.trigger === "abnormal" ? "abnormal return" : "size"}</Pill></li>
          {m.drift?.label_5 && <li><Pill>Next 5 sessions: {DRIFT[m.drift.label_5] ?? m.drift.label_5}</Pill></li>}
          {m.decomposition?.dominant && <li><Pill><DriverChip driver={m.decomposition.dominant} /></Pill></li>}
        </ul>
      </header>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,8fr)_minmax(0,4fr)]">
        <div className="grid content-start gap-6">
          <Panel className="p-5" aria-labelledby="breakdown">
            <SectionTitle id="breakdown" aside="Percentage points">Breakdown</SectionTitle>
            <DecompositionBar decomposition={m.decomposition} total={m.daily_return} warning={m.decomposition_warning} />
          </Panel>
          <Panel className="p-5" aria-labelledby="timeline">
            <SectionTitle id="timeline" aside="New York time">When the news broke</SectionTitle>
            <SessionTimeline movement={m} frame={data.session} />
          </Panel>
          {primaryFiling && (
            <Panel className="p-5" aria-labelledby="primary">
              <SectionTitle id="primary">Primary source</SectionTitle>
              <div id={`F-${primaryFiling.accession}`}>
                <p className="text-body-lg text-ink">SEC {primaryFiling.form}{primaryFiling.events.length ? `: ${primaryFiling.events.join(", ")}` : ""}</p>
                <p className="mt-1 text-caption text-mute">Filed {etDateTime(primaryFiling.accepted_at)}{primaryFiling.timing ? `, ${timingLabel[primaryFiling.timing].toLowerCase()}` : ""}. Accession {primaryFiling.accession}</p>
                {primaryFiling.rationale && <p className="mt-2 text-body text-ink">{primaryFiling.rationale}</p>}
                {primaryFiling.url && <a className="mt-3 inline-block text-body underline underline-offset-2" href={primaryFiling.url} target="_blank" rel="noreferrer">Read it on sec.gov</a>}
              </div>
            </Panel>
          )}
          <Panel className="p-5" aria-labelledby="evidence">
            <SectionTitle id="evidence" aside={m.news.length ? "Strongest first" : undefined}>Evidence</SectionTitle>
            {m.evidence_abstained && <p className="mb-3 rounded-control bg-surface-2 px-3 py-2 text-body text-ink">No clear driver: {m.abstain_reason ?? "nothing found explains this move."}</p>}
            {m.news.length ? (
              <ol className="grid gap-4">{m.news.map((link, i) => <EvidenceItem key={link.article.id} link={link} rank={i + 1} />)}</ol>
            ) : <EvidenceLine status={m.evidence} />}
            {m.filings.filter((f) => f !== primaryFiling).map((f) => (
              <p key={f.accession} id={`F-${f.accession}`} className="mt-3 text-body text-ink">
                SEC {f.form}{f.events.length ? `: ${f.events.join(", ")}` : ""} <span className="text-caption text-mute">{etDateTime(f.accepted_at)}</span>
                {f.url && <> <a className="text-caption underline" href={f.url} target="_blank" rel="noreferrer">sec.gov</a></>}
              </p>
            ))}
          </Panel>
        </div>
        <aside className="grid content-start gap-6" aria-label="Facts">
          <WhyFlagged data={data} />
          <References title="Peers that session" rows={data.peers} empty="No peers with stored prices." />
          <References title="Benchmarks that session" rows={data.benchmarks} empty="No benchmark prices stored." />
          {m.drift && (
            <Panel className="p-4" aria-labelledby="next">
              <SectionTitle id="next">What happened next</SectionTitle>
              <dl className="grid gap-1 text-body">
                <Row label="Next 5 sessions">{pp(m.drift.car_5)} {m.drift.label_5 ? `(${(DRIFT[m.drift.label_5] ?? m.drift.label_5).toLowerCase()})` : ""}</Row>
                <Row label="Next 20 sessions">{pp(m.drift.car_20)} {m.drift.label_20 ? `(${(DRIFT[m.drift.label_20] ?? m.drift.label_20).toLowerCase()})` : ""}</Row>
              </dl>
              <p className="mt-2 text-caption text-mute">Abnormal return after the move, with the model fixed on the day.</p>
            </Panel>
          )}
          <nav aria-label="Other moves" className="flex justify-between text-body">
            {data.previous_date ? <Link className="underline underline-offset-2" to={`/t/${symbol}/m/${data.previous_date}`}>Previous move, {longDate(data.previous_date)}</Link> : <span />}
            {data.next_date && <Link className="underline underline-offset-2" to={`/t/${symbol}/m/${data.next_date}`}>Next, {longDate(data.next_date)}</Link>}
          </nav>
        </aside>
      </div>
    </article>
  );
}

function EvidenceItem({ link, rank }: { link: LinkedArticle; rank: number }) {
  return (
    <li id={`A-${link.article.id}`} className="grid grid-cols-[24px_1fr] gap-2">
      <span className="num pt-0.5 text-body text-mute">{rank}</span>
      <div>
        <a href={link.article.url} target="_blank" rel="noreferrer" className="text-body-lg font-medium text-ink hover:underline">{link.article.title ?? link.article.url}</a>
        <p className="mt-0.5 flex flex-wrap gap-x-3 text-caption text-mute">
          <span>{link.article.source ?? "Unknown source"}</span>
          {link.article.published_at && <span>{etDateTime(link.article.published_at)}</span>}
          {link.timing && <span>{timingLabel[link.timing]}</span>}
          {link.role && <span className="font-medium text-ink">{ROLE[link.role] ?? link.role}</span>}
          <span>{tierLabel[link.relevance_tier] ?? link.relevance_tier} news</span>
          <span className="num">Score {plain(link.relevance_score, 2)}</span>
        </p>
        {link.rationale && <p className="mt-1 text-body text-ink">{link.rationale}</p>}
      </div>
    </li>
  );
}

function WhyFlagged({ data }: { data: MovementCase }) {
  const d = data.detection;
  const m = data.movement;
  return (
    <Panel className="p-4" aria-labelledby="flagged">
      <SectionTitle id="flagged">Why this was flagged</SectionTitle>
      <dl className="grid gap-1 text-body">
        <Row label="Size test">{d.raw.passed ? "Passed" : "Not passed"}: |{pct(d.raw.daily_return, 2).replace(/^[+−]/, "")}| against {pct(d.raw.threshold, 2).replace("+", "")} ({d.raw.threshold_source === "floor" ? "the floor" : `${d.raw.k} × ${d.raw.window}-day volatility`})</Row>
        <Row label="Abnormal test">{d.abnormal.passed == null ? "Not decomposed" : `${d.abnormal.passed ? "Passed" : "Not passed"}: |SAR| ${plain(Math.abs(d.abnormal.sar ?? 0))} against ${d.abnormal.k}`}</Row>
        {m.decomposition && <Row label="Model">{m.decomposition.estimation_sessions} sessions against {[m.decomposition.market_benchmark, m.decomposition.sector_benchmark].filter(Boolean).join(" and ")}{m.decomposition.r_squared != null ? `, R² ${plain(m.decomposition.r_squared, 2)}` : ""}</Row>}
        <Row label="Detection now">{d.current_mode === "raw" ? "Size only" : d.current_mode === "abnormal" ? "Abnormal return only" : "Either test"}</Row>
      </dl>
      <p className="mt-2 text-caption text-mute">SAR is a heuristic size in standard deviations, not a p-value.</p>
    </Panel>
  );
}

function References({ title, rows, empty }: { title: string; rows: MovementCase["peers"]; empty: string }) {
  const id = title.replace(/\W+/g, "-").toLowerCase();
  return (
    <Panel className="p-4" aria-labelledby={id}>
      <SectionTitle id={id}>{title}</SectionTitle>
      {rows.length ? (
        <ul className="grid gap-1">
          {rows.map((r) => (
            <li key={`${r.symbol}-${r.name}-${r.role}`} className="flex items-center justify-between gap-3 text-body">
              <span className="min-w-0 truncate">{r.symbol ? <Link to={`/t/${r.symbol}`} className="mono font-medium hover:underline">{r.symbol}</Link> : null} <span className="text-mute">{r.name ?? r.role}</span></span>
              {r.daily_return == null ? <span className="text-caption text-mute">no prices</span> : <Change value={r.daily_return} className={cx("text-body")} />}
            </li>
          ))}
        </ul>
      ) : <p className="text-body text-mute">{empty}</p>}
    </Panel>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="grid grid-cols-[120px_1fr] gap-2"><dt className="text-mute">{label}</dt><dd className="m-0 text-ink">{children}</dd></div>;
}

function setMeta(name: string, content: string) {
  let tag = document.querySelector(`meta[name="${name}"]`);
  if (!tag) {
    tag = document.createElement("meta");
    tag.setAttribute("name", name);
    document.head.appendChild(tag);
  }
  tag.setAttribute("content", content);
}
