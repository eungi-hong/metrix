/**
 * The session timeline: five buckets of fixed relative width (before the
 * prior close, overnight, during the session, after the close, later), each
 * item placed proportionally inside its bucket by its time. Fixed widths read
 * better than true time, which would make the overnight gap dominate; the
 * boundaries are labelled with their real times. Items known only by date
 * never get an invented hour: they go in a lane of their own.
 */

export type Bucket = "before_prior_close" | "overnight" | "intraday" | "after_close" | "later";
export const BUCKETS: Bucket[] = ["before_prior_close", "overnight", "intraday", "after_close", "later"];
export const WIDTH: Record<Bucket, number> = { before_prior_close: 15, overnight: 25, intraday: 35, after_close: 15, later: 10 };
/** Appendix B: can news in this bucket have caused the close-to-close move? */
export const CAN_EXPLAIN: Record<Bucket, "Yes" | "Weakly" | "No"> = {
  before_prior_close: "Weakly", overnight: "Yes", intraday: "Yes", after_close: "No", later: "No",
};
export const BUCKET_LABEL: Record<Bucket, string> = {
  before_prior_close: "Before the prior close", overnight: "Overnight", intraday: "Session", after_close: "After close", later: "Later",
};

export interface Frame {
  prior_close: string;
  open: string;
  close: string;
  next_open: string;
}

const DAY = 24 * 3600 * 1000;

/** Left offset (0..100) of the bucket's start. */
export function bucketStart(bucket: Bucket): number {
  let at = 0;
  for (const b of BUCKETS) {
    if (b === bucket) return at;
    at += WIDTH[b];
  }
  return at;
}

/** Where an instant sits on the timeline, 0..100, or null outside its bucket's range. */
export function position(instant: string, bucket: Bucket, frame: Frame): number {
  const t = Date.parse(instant);
  const [priorClose, open, close, nextOpen] = [frame.prior_close, frame.open, frame.close, frame.next_open].map(Date.parse);
  const spans: Record<Bucket, [number, number]> = {
    before_prior_close: [priorClose - DAY, priorClose],
    overnight: [priorClose, open],
    intraday: [open, close],
    after_close: [close, nextOpen],
    later: [nextOpen, nextOpen + DAY],
  };
  const [from, to] = spans[bucket];
  const fraction = Math.min(Math.max((t - from) / (to - from), 0.04), 0.96);
  return bucketStart(bucket) + fraction * WIDTH[bucket];
}

export interface TimelineItem {
  id: string;
  kind: "article" | "filing";
  label: string;
  at: string | null;
  timing: Bucket | null;
  precision: "time" | "day" | null;
}

export function lanes(items: TimelineItem[]): { timed: TimelineItem[]; dateOnly: TimelineItem[] } {
  return {
    timed: items.filter((i) => i.at && i.timing && i.precision === "time"),
    dateOnly: items.filter((i) => !(i.at && i.timing && i.precision === "time")),
  };
}
