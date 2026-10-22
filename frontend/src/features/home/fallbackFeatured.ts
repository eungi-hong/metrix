/**
 * A featured move the home page can find on its own, from the pre-warmed
 * tickers, when the pulse has none to offer: an API too old to return
 * `featured`, or a stretch where every recent move is still waiting for
 * news. Same rule as the API's pick: the largest move whose strongest
 * evidence scores at least FEATURED_MIN_SCORE.
 */
import { useQueries } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { Movement, PulseMover, TickerDetail } from "../../api/types";

export const SHOWCASE_SYMBOLS = ["TSLA", "NVDA", "AMZN", "AAPL", "MSFT", "NKE", "SBUX"];
export const FEATURED_MIN_SCORE = 0.7;

function strongest(movement: Movement): number {
  return movement.news.reduce((best, link) => Math.max(best, link.relevance_score), 0);
}

export function pickFeatured(details: (TickerDetail | undefined)[]): PulseMover | null {
  let best: { detail: TickerDetail; movement: Movement } | null = null;
  for (const detail of details) {
    for (const movement of detail?.movements ?? []) {
      if (movement.evidence?.state !== "explained" || strongest(movement) < FEATURED_MIN_SCORE) continue;
      if (!best || Math.abs(movement.daily_return) > Math.abs(best.movement.daily_return)) best = { detail: detail!, movement };
    }
  }
  if (!best) return null;
  const { detail, movement } = best;
  const top = [...movement.news].sort((a, b) => b.relevance_score - a.relevance_score)[0];
  const title = top?.article.title ?? top?.article.url ?? "";
  return {
    symbol: detail.ticker.symbol,
    company_name: detail.ticker.company_name,
    date: movement.date,
    daily_return: movement.daily_return,
    abnormal_return: movement.decomposition?.abnormal ?? null,
    sar: movement.decomposition?.sar ?? null,
    dominant: movement.decomposition?.dominant ?? null,
    explanation: top?.rationale ? `${title}: ${top.rationale}` : title,
    evidence: "explained",
  };
}

export function useFallbackFeatured(enabled: boolean): PulseMover | null {
  const results = useQueries({
    queries: SHOWCASE_SYMBOLS.map((symbol) => ({
      queryKey: ["featured-fallback", symbol],
      queryFn: () => api.ticker(symbol, { include_prices: false, include_benchmarks: false, sort: "abs_return", limit: 10 }),
      enabled,
      retry: false,
      staleTime: 300_000,
    })),
  });
  return enabled ? pickFeatured(results.map((r) => r.data)) : null;
}
