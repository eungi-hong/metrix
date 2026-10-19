/** Filtering and sorting the movements table, from the URL's query string. */
import type { Movement } from "../../api/types";

export type Sort = "date" | "abs_return" | "abs_sar" | "rvol";
export const SORTS: Sort[] = ["date", "abs_return", "abs_sar", "rvol"];
export const SORT_LABEL: Record<Sort, string> = { date: "Newest", abs_return: "Largest move", abs_sar: "Most unusual", rvol: "Highest volume" };

export interface Filters {
  direction: "" | "up" | "down";
  trigger: "" | "raw" | "abnormal" | "both";
  dominant: "" | "market" | "sector" | "company";
  evidence: "" | "explained" | "pending";
  minSar: number | null;
  sort: Sort;
}

export function readFilters(params: URLSearchParams): Filters {
  const pick = <T extends string>(name: string, allowed: readonly T[]): T | "" => {
    const v = params.get(name) as T | null;
    return v && allowed.includes(v) ? v : "";
  };
  const minSar = Number(params.get("min_sar"));
  const sort = params.get("sort") as Sort | null;
  return {
    direction: pick("direction", ["up", "down"] as const),
    trigger: pick("trigger", ["raw", "abnormal", "both"] as const),
    dominant: pick("dominant", ["market", "sector", "company"] as const),
    evidence: pick("evidence", ["explained", "pending"] as const),
    minSar: params.get("min_sar") && Number.isFinite(minSar) && minSar > 0 ? minSar : null,
    sort: sort && SORTS.includes(sort) ? sort : "date",
  };
}

export function hasEvidence(m: Movement): boolean {
  return m.evidence?.state === "explained";
}

export function applyFilters(movements: Movement[], f: Filters): Movement[] {
  const rows = movements.filter((m) =>
    (!f.direction || m.direction === f.direction)
    && (!f.trigger || (m.trigger ?? "raw") === f.trigger)
    && (!f.dominant || m.decomposition?.dominant === f.dominant)
    && (!f.evidence || (f.evidence === "explained" ? hasEvidence(m) : !hasEvidence(m)))
    && (f.minSar == null || Math.abs(m.decomposition?.sar ?? 0) >= f.minSar));
  const key: Record<Sort, (m: Movement) => number | null> = {
    date: (m) => Date.parse(m.date),
    abs_return: (m) => Math.abs(m.daily_return),
    abs_sar: (m) => (m.decomposition?.sar == null ? null : Math.abs(m.decomposition.sar)),
    rvol: (m) => m.volume_analysis?.relative_volume ?? null,
  };
  const get = key[f.sort];
  // Largest first; rows without the value go last; ties by date, newest first.
  return [...rows].sort((a, b) => {
    const x = get(a);
    const y = get(b);
    if (x == null && y == null) return b.date.localeCompare(a.date);
    if (x == null) return 1;
    if (y == null) return -1;
    return y - x || b.date.localeCompare(a.date);
  });
}

export function summary(rows: Movement[]): string {
  if (!rows.length) return "No moves";
  const down = rows.filter((m) => m.direction === "down").length;
  const sars = rows.map((m) => m.decomposition?.sar).filter((v): v is number => v != null).map(Math.abs).sort((a, b) => a - b);
  const median = sars.length ? sars[Math.floor((sars.length - 1) / 2)] : null;
  const explained = rows.filter(hasEvidence).length;
  const parts = [`${rows.length} ${rows.length === 1 ? "move" : "moves"}`, `${down} down`];
  if (median != null) parts.push(`median |SAR| ${median.toFixed(1)}`);
  parts.push(`${explained} explained`);
  return parts.join(", ");
}

export function topLink(m: Movement) {
  return m.news[0] ?? null;
}
