import { describe, expect, it } from "vitest";
import { BUCKETS, WIDTH, bucketStart, lanes, position } from "./timeline";

const frame = { prior_close: "2026-08-26T20:00:00Z", open: "2026-08-27T13:30:00Z", close: "2026-08-27T20:00:00Z", next_open: "2026-08-28T13:30:00Z" };

describe("the session timeline", () => {
  it("has buckets that fill the width", () => {
    expect(BUCKETS.reduce((sum, b) => sum + WIDTH[b], 0)).toBe(100);
    expect(bucketStart("intraday")).toBe(40);
  });
  it("places an item proportionally inside its bucket", () => {
    // 16:20 ET, after the prior close: early in the overnight bucket (15..40)
    const p = position("2026-08-26T20:20:00Z", "overnight", frame);
    expect(p).toBeGreaterThan(15);
    expect(p).toBeLessThan(17);
    // the middle of the session
    expect(position("2026-08-27T16:45:00Z", "intraday", frame)).toBeCloseTo(57.5, 1);
  });
  it("keeps items inside their bucket's edges", () => {
    expect(position("2026-08-20T00:00:00Z", "before_prior_close", frame)).toBeCloseTo(0.6, 1);
  });
  it("puts date-only items in their own lane", () => {
    const { timed, dateOnly } = lanes([
      { id: "a", kind: "article", label: "a", at: "2026-08-27T15:00:00Z", timing: "intraday", precision: "time" },
      { id: "b", kind: "article", label: "b", at: "2026-08-27T00:00:00Z", timing: "intraday", precision: "day" },
      { id: "c", kind: "filing", label: "c", at: null, timing: null, precision: null },
    ]);
    expect(timed.map((i) => i.id)).toEqual(["a"]);
    expect(dateOnly.map((i) => i.id)).toEqual(["b", "c"]);
  });
});
