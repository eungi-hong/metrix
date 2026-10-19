import { RefreshCw } from "lucide-react";
import { useEarnings } from "../../api/queries";
import type { TickerDetail } from "../../api/types";
import { Button, Change, Pill } from "../../design/ui";
import { longDate, money, relativeAge } from "../../lib/format";

export function TickerHeader({ data, onRefresh, refreshing }: { data: TickerDetail; onRefresh: () => void; refreshing: boolean }) {
  const prices = data.prices ?? [];
  const last = prices.at(-1);
  const prev = prices.at(-2);
  const change = last && prev ? last.adj_close / prev.adj_close - 1 : null;
  const latestRegime = data.movements.find((m) => m.regime?.percentile != null)?.regime;
  const earnings = useEarnings(data.ticker.symbol);
  const lastEarnings = earnings.data?.events[0];
  return (
    <header className="grid gap-2">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <h1 className="text-title text-ink"><span className="mono">{data.ticker.symbol}</span> <span className="font-normal text-mute">{data.ticker.company_name}</span></h1>
        {last && <span className="num text-display-sm text-ink">{money(last.close ?? last.adj_close, data.ticker.currency ?? "USD")}</span>}
        {change != null && <Change value={change} digits={2} className="text-heading" />}
        {last && <span className="text-caption text-mute">Close on {longDate(last.date)}</span>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {data.ticker.sector && <Pill>{data.ticker.sector}</Pill>}
        {data.ticker.industry && <Pill>{data.ticker.industry}</Pill>}
        {lastEarnings?.session && <Pill>Last earnings {longDate(lastEarnings.session)}</Pill>}
        {latestRegime?.percentile != null && <Pill>Volatility: {ordinal(Math.round(latestRegime.percentile))} percentile of the past year</Pill>}
        <span className="ml-auto flex items-center gap-2 text-caption text-mute">
          Updated {relativeAge(data.last_ingested_at)}
          <Button variant="quiet" className="h-7 px-2" onClick={onRefresh} disabled={refreshing} aria-label="Fetch the latest prices and news">
            <RefreshCw size={14} aria-hidden className={refreshing ? "motion-safe:animate-spin" : ""} /> Refresh
          </Button>
        </span>
      </div>
    </header>
  );
}

function ordinal(n: number): string {
  const tail = n % 100 >= 11 && n % 100 <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th";
  return `${n}${tail}`;
}
