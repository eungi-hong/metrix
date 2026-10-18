import { describe, expect, it } from "vitest";
import type { Movement } from "../../api/types";
import { applyFilters, readFilters, summary } from "./table";
import { buildMarkers, markerSize, rebase } from "./markers";

const base = { news: [], filings: [], evidence: { state: "explained", detail: "" } } as unknown as Movement;
function move(date: string, ret: number, extra: Record<string, unknown> = {}): Movement {
  return { ...base, date, daily_return: ret, direction: ret < 0 ? "down" : "up", ...extra } as Movement;
}
const rows = [
  move("2026-01-02", -0.08, { trigger: "both", decomposition: { sar: -5.1, dominant: "company" }, volume_analysis: { relative_volume: 2 } }),
  move("2026-02-03", 0.04, { trigger: "raw", decomposition: { sar: 2.0, dominant: "sector" }, evidence: { state: "queued" } }),
  move("2026-03-04", 0.06, { decomposition: null }),
];

describe("the movements table", () => {
  it("reads filters from the URL, ignoring junk", () => {
    const f = readFilters(new URLSearchParams("direction=down&trigger=nope&min_sar=2.5&sort=abs_sar"));
    expect(f).toMatchObject({ direction: "down", trigger: "", minSar: 2.5, sort: "abs_sar" });
    expect(readFilters(new URLSearchParams("")).sort).toBe("date");
  });
  it("filters and sorts, with missing values last", () => {
    const f = readFilters(new URLSearchParams(""));
    expect(applyFilters(rows, f).map((m) => m.date)).toEqual(["2026-03-04", "2026-02-03", "2026-01-02"]);
    expect(applyFilters(rows, { ...f, sort: "abs_sar" }).map((m) => m.date)).toEqual(["2026-01-02", "2026-02-03", "2026-03-04"]);
    expect(applyFilters(rows, { ...f, trigger: "raw" }).map((m) => m.date)).toEqual(["2026-03-04", "2026-02-03"]); // no trigger stored: raw
    expect(applyFilters(rows, { ...f, evidence: "pending" }).map((m) => m.date)).toEqual(["2026-02-03"]);
    expect(applyFilters(rows, { ...f, minSar: 3 }).map((m) => m.date)).toEqual(["2026-01-02"]);
  });
  it("summarises the rows", () => {
    expect(summary(rows)).toBe("3 moves, 1 down, median |SAR| 2.0, 2 explained");
    expect(summary([])).toBe("No moves");
  });
});

describe("chart markers", () => {
  const palette = { market: "M", sector: "S", company: "C", neutral: "N", filing: "F" };
  it("show direction by shape, driver by colour and size by SAR", () => {
    const [down] = buildMarkers([rows[0]], "1Y", palette);
    expect(down).toMatchObject({ shape: "arrowDown", position: "aboveBar", color: "C", size: 1.5 });
    expect(markerSize(6.2)).toBe(2);
    expect(markerSize(null)).toBe(1);
  });
  it("thin out on the longest range, but keep earnings", () => {
    const withFilings = [{ ...rows[1], filings: [{ accession: "a", items: ["2.02"] }, { accession: "b", items: ["5.02"] }] } as unknown as Movement];
    expect(buildMarkers(withFilings, "1Y", palette).map((m) => m.text ?? m.shape)).toEqual(["arrowUp", "E", "F"]);
    expect(buildMarkers(withFilings, "All", palette).map((m) => m.text ?? m.shape)).toEqual(["E"]);
  });
  it("rebases benchmarks to 100", () => {
    expect(rebase([{ date: "a", value: 50 }, { date: "b", value: 55 }])).toEqual([{ time: "a", value: 100 }, { time: "b", value: 110.00000000000001 }]);
  });
});
