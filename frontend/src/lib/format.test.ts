import { describe, expect, it } from "vitest";
import { MINUS, etDateTime, etTime, glyph, localTime, longDate, pct, pctPoints, pp, shortDate, signedNumber, times } from "./format";
import { axisExtent, axisPosition, dominant, waterfall } from "./decomposition";

describe("returns", () => {
  it("always carry a sign and use the true minus", () => {
    expect(pct(0.0871)).toBe("+8.7%");
    expect(pct(-0.0624)).toBe(`${MINUS}6.2%`);
    expect(pct(0)).toBe("0.0%");
    expect(pct(-0.00001)).toBe("0.0%"); // rounds to zero: no sign
    expect(pct(0.12345, 2)).toBe("+12.35%");
    expect(pct(null)).toBe("n/a");
    expect(pp(0.079)).toBe("+7.9 pp");
    expect(pp(-0.003)).toBe(`${MINUS}0.3 pp`);
    expect(pctPoints(-19.88, 2)).toBe(`${MINUS}19.88%`);
    expect(signedNumber(5.24)).toBe("+5.2");
    expect(times(3.14)).toBe("3.1×");
    expect([glyph(1), glyph(-1), glyph(0)]).toEqual(["▲", "▼", ""]);
  });
});

describe("dates", () => {
  it("are calendar days, never shifted by the viewer's zone", () => {
    expect(longDate("2026-08-27")).toBe("Aug 27, 2026");
    expect(shortDate("2026-01-09")).toBe("Jan 9");
  });
  it("market times are New York time, labelled", () => {
    expect(etTime("2026-08-26T20:05:00Z")).toBe("4:05 pm ET");
    expect(etDateTime("2026-01-09T14:30:00Z")).toBe("Jan 9, 9:30 am ET");
  });
  it("resets are in the viewer's zone", () => {
    expect(localTime("2026-10-20T00:00:00Z", "America/Los_Angeles")).toBe("5:00 pm PDT");
    expect(localTime("2026-10-20T00:00:00Z", "Europe/London")).toMatch(/^1:00 am (BST|GMT\+1)$/);
  });
});

describe("the decomposition waterfall", () => {
  const d = { alpha: 0.0004, market: 0.011, sector: -0.003, abnormal: 0.0786 };
  it("sums exactly to the return, each part starting where the last ended", () => {
    const w = waterfall(d);
    expect(w.total).toBeCloseTo(0.087, 10);
    expect(w.segments.map((s) => s.part)).toEqual(["baseline", "market", "sector", "company"]);
    for (let i = 1; i < w.segments.length; i += 1) expect(w.segments[i].start).toBe(w.segments[i - 1].end);
    expect(w.segments[2].end).toBeLessThan(w.segments[2].start); // a negative part runs left
  });
  it("tracks the extremes for a symmetric axis", () => {
    const w = waterfall({ alpha: 0, market: -0.02, sector: 0.01, abnormal: -0.05 });
    expect(w.min).toBeCloseTo(-0.06);
    expect(w.max).toBe(0);
    expect(axisExtent(w)).toBeCloseTo(0.066);
    expect(axisPosition(0, 0.066)).toBe(50);
    expect(axisPosition(-0.066, 0.066)).toBe(0);
  });
  it("names the dominant part", () => {
    expect(dominant(d)).toBe("company");
    expect(dominant({ alpha: 0, market: -0.04, sector: 0.01, abnormal: 0.02 })).toBe("market");
    expect(dominant({ alpha: null, market: null, sector: null, abnormal: null })).toBeNull();
  });
});
