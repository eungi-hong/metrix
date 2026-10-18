/** An answer: sanitised markdown with inline citation chips. */
import * as HoverCard from "@radix-ui/react-hover-card";
import { FileText, Newspaper, TrendingUp } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Link } from "react-router-dom";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import type { ArticleSource, ChatSources, FilingSource, MovementSource } from "../../api/types";
import { etDateTime, longDate, pctPoints, plain, tierLabel } from "../../lib/format";
import { knownLabels, linkCitations } from "./citations";

type Source = { kind: "M"; s: MovementSource } | { kind: "A"; s: ArticleSource } | { kind: "F"; s: FilingSource };

export function sourceFor(label: string, sources: ChatSources | null | undefined): Source | null {
  const m = sources?.movements.find((x) => x.ref === label);
  if (m) return { kind: "M", s: m };
  const a = sources?.articles.find((x) => x.ref === label);
  if (a) return { kind: "A", s: a };
  const f = sources?.filings?.find((x) => x.ref === label);
  if (f) return { kind: "F", s: f };
  return null;
}

export function hrefFor(source: Source): { to: string; external: boolean } {
  if (source.kind === "M") return { to: `/t/${source.s.symbol}/m/${source.s.date}`, external: false };
  if (source.kind === "A") return { to: source.s.url, external: true };
  return { to: source.s.url ?? `https://www.sec.gov/edgar/search/#/q=%22${source.s.accession}%22`, external: true };
}

const ICON = { M: TrendingUp, A: Newspaper, F: FileText };

export function CitationChip({ label, sources }: { label: string; sources: ChatSources | null | undefined }) {
  const source = sourceFor(label, sources);
  if (!source) return <span className="mono text-caption text-mute">{label}</span>;
  const Icon = ICON[source.kind];
  const { to, external } = hrefFor(source);
  const chip = (
    <span className="mono mx-0.5 inline-flex h-5 min-w-6 items-center gap-1 rounded border border-line bg-surface-2 px-1 align-[1px] text-[11px] text-ink hover:border-line-strong">
      <Icon size={11} aria-hidden />{label}
    </span>
  );
  return (
    <HoverCard.Root openDelay={150} closeDelay={80}>
      <HoverCard.Trigger asChild>
        {external
          ? <a href={to} target="_blank" rel="noreferrer" aria-label={`Source ${label}: ${describe(source)}`}>{chip}</a>
          : <Link to={to} aria-label={`Source ${label}: ${describe(source)}`}>{chip}</Link>}
      </HoverCard.Trigger>
      <HoverCard.Portal>
        <HoverCard.Content sideOffset={6} className="pop-in z-50 w-[300px] rounded-card border border-line bg-surface p-3 text-body shadow-pop">
          <SourcePreview source={source} />
        </HoverCard.Content>
      </HoverCard.Portal>
    </HoverCard.Root>
  );
}

function describe(source: Source): string {
  if (source.kind === "M") return `${source.s.symbol} on ${longDate(source.s.date)}, ${pctPoints(source.s.daily_return_pct)}`;
  if (source.kind === "A") return source.s.title ?? source.s.url;
  return `SEC ${source.s.form}`;
}

export function SourcePreview({ source }: { source: Source }) {
  if (source.kind === "M") {
    return (<>
      <p className="text-caption text-mute">Move, opens the case file</p>
      <p className="mt-0.5 font-medium text-ink"><span className="mono">{source.s.symbol}</span> {longDate(source.s.date)}</p>
      <p className="num text-ink">{pctPoints(source.s.daily_return_pct, 2)}</p>
    </>);
  }
  if (source.kind === "A") {
    return (<>
      <p className="text-caption text-mute">Article{source.s.source ? `, ${source.s.source}` : ""}{source.s.published_at ? `, ${longDate(source.s.published_at)}` : ""}</p>
      <p className="mt-0.5 font-medium text-ink">{source.s.title ?? source.s.url}</p>
      {source.s.relevance_score != null && <p className="num mt-1 text-caption text-mute">{source.s.relevance_tier ? `${tierLabel[source.s.relevance_tier] ?? source.s.relevance_tier} news, ` : ""}score {plain(source.s.relevance_score, 2)}</p>}
    </>);
  }
  return (<>
    <p className="text-caption text-mute">SEC filing, opens sec.gov</p>
    <p className="mt-0.5 font-medium text-ink"><span className="mono">{source.s.symbol}</span> {source.s.form}{source.s.items.length ? `, items ${source.s.items.join(", ")}` : ""}</p>
    {source.s.accepted_at && <p className="text-caption text-mute">Filed {etDateTime(source.s.accepted_at)}</p>}
  </>);
}

export function Answer({ text, sources, final }: { text: string; sources: ChatSources | null | undefined; final: boolean }) {
  const prepared = linkCitations(text, final, final ? knownLabels(sources) : undefined);
  return (
    <div className="prose-answer text-body-lg text-ink">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        components={{
          a: ({ href, children }) => {
            if (href?.startsWith("#cite-")) return <CitationChip label={href.slice(6)} sources={sources} />;
            if (href?.startsWith("#pending-")) return <span className="mono text-caption text-mute">[{href.slice(9)}]</span>;
            return <a href={href} target="_blank" rel="noreferrer" className="underline underline-offset-2">{children}</a>;
          },
        }}
      >
        {prepared}
      </ReactMarkdown>
    </div>
  );
}

/** Cited sources only, grouped. */
export function SourceCards({ sources }: { sources: ChatSources }) {
  const groups: [string, Source[]][] = [
    ["Moves", sources.movements.map((s) => ({ kind: "M" as const, s }))],
    ["Filings", (sources.filings ?? []).map((s) => ({ kind: "F" as const, s }))],
    ["Articles", sources.articles.map((s) => ({ kind: "A" as const, s }))],
  ];
  const total = groups.reduce((n, [, g]) => n + g.length, 0);
  if (!total) return null;
  return (
    <section className="mt-4" aria-label="Sources cited">
      <p className="text-caption text-mute">Sources cited ({total})</p>
      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        {groups.flatMap(([, g]) => g).map((source) => {
          const { to, external } = hrefFor(source);
          const body = <span className="block rounded-control border border-line bg-surface px-3 py-2 hover:border-line-strong"><span className="mono mr-1 text-caption text-mute">{source.s.ref}</span><SourcePreviewInline source={source} /></span>;
          return external
            ? <a key={source.s.ref} href={to} target="_blank" rel="noreferrer">{body}</a>
            : <Link key={source.s.ref} to={to}>{body}</Link>;
        })}
      </div>
    </section>
  );
}

function SourcePreviewInline({ source }: { source: Source }) {
  if (source.kind === "M") return <span className="text-body text-ink"><span className="mono">{source.s.symbol}</span> {longDate(source.s.date)} <span className="num">{pctPoints(source.s.daily_return_pct)}</span></span>;
  if (source.kind === "A") return <span className="text-body text-ink">{source.s.title ?? source.s.url} <span className="text-caption text-mute">{source.s.source}</span></span>;
  return <span className="text-body text-ink">SEC {source.s.form} <span className="text-caption text-mute">{source.s.symbol}</span></span>;
}
