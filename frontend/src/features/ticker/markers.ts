/**
 * Chart markers for movements, filings and earnings. Direction is carried by
 * shape (an up arrow below the bar, a down arrow above), the dominant part of
 * the move by colour, and how unusual it was by size. Long ranges show only
 * the strongest moves and earnings filings, so markers never pile up; the
 * table below the chart is never filtered by this.
 */
import type { Movement } from "../../api/types";

export type Range = "1M" | "3M" | "6M" | "1Y" | "All";
export const RANGE_BARS: Record<Range, number | null> = { "1M": 22, "3M": 63, "6M": 126, "1Y": 252, All: null };
export const RANGES: Range[] = ["1M", "3M", "6M", "1Y", "All"];

export interface Palette {
  market: string;
  sector: string;
  company: string;
  neutral: string;
  filing: string;
}

export interface Marker {
  time: string;
  position: "aboveBar" | "belowBar" | "inBar";
  shape: "arrowUp" | "arrowDown" | "square" | "circle";
  color: string;
  size: number;
  text?: string;
  id: string;
}

export function markerSize(sar: number | null | undefined): number {
  const s = Math.abs(sar ?? 0);
  return s >= 6 ? 2 : s >= 4 ? 1.5 : 1;
}

export function buildMarkers(movements: Movement[], range: Range, palette: Palette): Marker[] {
  const dense = range === "All";
  const out: Marker[] = [];
  for (const m of movements) {
    const sar = m.decomposition?.sar ?? null;
    if (!dense || Math.abs(sar ?? 0) >= 4 || sar == null) {
      const up = m.daily_return >= 0;
      out.push({
        id: `m-${m.date}`,
        time: m.date,
        position: up ? "belowBar" : "aboveBar",
        shape: up ? "arrowUp" : "arrowDown",
        color: m.decomposition?.dominant ? palette[m.decomposition.dominant as "market" | "sector" | "company"] : palette.neutral,
        size: markerSize(sar),
      });
    }
    for (const f of m.filings) {
      const earnings = f.items.includes("2.02");
      if (dense && !earnings) continue;
      out.push({ id: `f-${f.accession}`, time: m.date, position: "inBar", shape: earnings ? "circle" : "square", color: palette.filing, size: 1, text: earnings ? "E" : "F" });
    }
  }
  // The chart needs markers in time order.
  return out.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
}

/** Rebase a series to 100 on its first point. */
export function rebase(points: { date: string; value: number }[]): { time: string; value: number }[] {
  const base = points[0]?.value;
  if (!base) return [];
  return points.map((p) => ({ time: p.date, value: (p.value / base) * 100 }));
}
