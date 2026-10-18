import { MINUS, pct, shortDate } from "../../lib/format";

/** "Why NVDA rose 8.7% on Aug 27", or, when nothing explains it, "NVDA rose 8.7% on Aug 27. No clear driver found". */
export function caseTitle(symbol: string, dailyReturn: number, date: string, explained: boolean): string {
  const verb = dailyReturn < 0 ? "fell" : "rose";
  const size = pct(Math.abs(dailyReturn)).replace("+", "").replace(MINUS, "");
  return explained ? `Why ${symbol} ${verb} ${size} on ${shortDate(date)}` : `${symbol} ${verb} ${size} on ${shortDate(date)}. No clear driver found`;
}
