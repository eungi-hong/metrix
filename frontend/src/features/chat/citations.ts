/**
 * Citations in answers. Labels (M412, A1033, F7) become markdown links to
 * `#cite-<label>` once the answer is final and the server has checked them;
 * while it streams they stay quiet grey text (`#pending-<label>`), because a
 * label the server later strips must not have looked like a source.
 */
import type { ChatSources, ToolCall } from "../../api/types";

const GROUP = /\[\s*([MAF]\d+(?:\s*[,;]\s*[MAF]\d+)*)\s*\]/g;

export function linkCitations(text: string, final: boolean, known?: Set<string>): string {
  return text.replace(GROUP, (_, inner: string) =>
    inner
      .split(/\s*[,;]\s*/)
      .map((label) => (final && (!known || known.has(label)) ? `[${label}](#cite-${label})` : final ? "" : `[${label}](#pending-${label})`))
      .join(""),
  );
}

export function knownLabels(sources: ChatSources | null | undefined): Set<string> {
  return new Set([...(sources?.movements ?? []), ...(sources?.articles ?? []), ...(sources?.filings ?? [])].map((s) => s.ref));
}

const NOUN: Record<string, [string, string]> = {
  get_movements: ["movement", "movements"],
  search_evidence: ["article", "articles"],
  get_filings: ["filing", "filings"],
  get_movement_case: ["case file", "case files"],
  compare: ["ticker compared", "tickers compared"],
  get_watchlist_digest: ["watchlist holding", "watchlist holdings"],
};

/** "Checked 3 movements and 2 filings" from the lookups and their row counts. */
export function activity(calls: { name: string; rows: number }[]): string {
  if (!calls.length) return "";
  const totals = new Map<string, number>();
  for (const c of calls) {
    const rows = c.name === "get_movement_case" ? 1 : c.rows;
    totals.set(c.name, (totals.get(c.name) ?? 0) + rows);
  }
  const parts = [...totals.entries()].map(([name, n]) => {
    const [one, many] = NOUN[name] ?? ["result", "results"];
    return `${n} ${n === 1 ? one : many}`;
  });
  const list = parts.length > 1 ? `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}` : parts[0];
  return `Checked ${list}`;
}

/** Rows a finished lookup returned, from its one-line summary ("3 of 12 NVDA movements"). */
export function rowsFromSummary(call: ToolCall): number {
  const match = call.summary.match(/^(\d+)/);
  return match ? Number(match[1]) : call.name === "get_movement_case" ? 1 : 0;
}
