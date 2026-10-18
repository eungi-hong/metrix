/**
 * The breakdown of one day's return into percentage points, as a zero-centred
 * waterfall: baseline (alpha), market, sector, company, each starting where
 * the previous ended, ending at the actual return. Parts can have opposite
 * signs, which is why this is never a 100% stacked bar.
 */

export type Part = "baseline" | "market" | "sector" | "company";

export interface Segment {
  part: Part;
  value: number; // fraction
  start: number; // fraction, cumulative before this part
  end: number;
}

export interface Waterfall {
  segments: Segment[];
  total: number; // the sum of the parts, equal to the actual return
  min: number;
  max: number;
}

export interface DecompositionInput {
  alpha: number | null | undefined;
  market: number | null | undefined;
  sector: number | null | undefined;
  abnormal: number | null | undefined;
}

export function waterfall(d: DecompositionInput): Waterfall {
  const parts: [Part, number][] = [
    ["baseline", d.alpha ?? 0],
    ["market", d.market ?? 0],
    ["sector", d.sector ?? 0],
    ["company", d.abnormal ?? 0],
  ];
  let running = 0;
  let min = 0;
  let max = 0;
  const segments: Segment[] = [];
  for (const [part, value] of parts) {
    const start = running;
    running += value;
    segments.push({ part, value, start, end: running });
    min = Math.min(min, start, running);
    max = Math.max(max, start, running);
  }
  return { segments, total: running, min, max };
}

/** Position of a value on a symmetric axis [-extent, +extent] as a 0..100 percentage. */
export function axisPosition(value: number, extent: number): number {
  if (extent <= 0) return 50;
  return 50 + (value / extent) * 50;
}

/** A symmetric extent that fits the waterfall with a little room, never zero. */
export function axisExtent(w: Waterfall): number {
  const raw = Math.max(Math.abs(w.min), Math.abs(w.max), 0.001);
  return raw * 1.1;
}

/** The part that dominates (largest absolute value among market, sector, company). */
export function dominant(d: DecompositionInput): Exclude<Part, "baseline"> | null {
  const candidates: [Exclude<Part, "baseline">, number | null | undefined][] = [
    ["market", d.market],
    ["sector", d.sector],
    ["company", d.abnormal],
  ];
  let best: Exclude<Part, "baseline"> | null = null;
  let size = -1;
  for (const [part, value] of candidates) {
    if (value != null && Math.abs(value) > size) {
      best = part;
      size = Math.abs(value);
    }
  }
  return best;
}
