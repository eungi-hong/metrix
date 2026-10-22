import { describe, expect, it } from "vitest";
import type { TickerDetail } from "../../api/types";
import { pickFeatured } from "./fallbackFeatured";

const move = (date: string, ret: number, state: string, score: number) => ({
  date, daily_return: ret, evidence: { state }, decomposition: { abnormal: ret / 2, sar: 3, dominant: "company" },
  news: [{ relevance_score: score, rationale: "why", article: { title: `t ${date}`, url: "u" } }],
});
const detail = (symbol: string, movements: unknown[]) => ({ ticker: { symbol, company_name: `${symbol} Inc` }, movements }) as unknown as TickerDetail;

describe("pickFeatured", () => {
  it("picks the largest strongly explained move across tickers", () => {
    const picked = pickFeatured([
      detail("AAA", [move("2026-07-01", -0.2, "failed", 0.9), move("2026-07-02", 0.05, "explained", 0.9)]),
      detail("BBB", [move("2026-07-23", -0.145, "explained", 0.95), move("2026-06-01", 0.3, "explained", 0.5)]),
      undefined,
    ]);
    expect(picked).toMatchObject({ symbol: "BBB", date: "2026-07-23", evidence: "explained", dominant: "company", explanation: "t 2026-07-23: why" });
  });
  it("returns null when nothing is strongly explained", () => {
    expect(pickFeatured([detail("AAA", [move("2026-07-01", -0.2, "failed", 0.9)])])).toBeNull();
  });
});
