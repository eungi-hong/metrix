/**
 * /t/:symbol: the chart and the selected move side by side, the moves table
 * below. A ticker Metrix has not researched starts researching on arrival,
 * with live progress; stored data is always shown when there is any.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { describeError } from "../../api/errors";
import { isWorking, keys, useLiveTicker, useTicker } from "../../api/queries";
import { Empty, Notice, Panel, Skeleton } from "../../design/ui";
import { useMediaHeight } from "./useMediaHeight";
import { MovementsTable } from "./MovementsTable";
import { PriceChart } from "./PriceChart";
import { Progress } from "./Progress";
import { SelectedMove } from "./SelectedMove";
import { TickerHeader } from "./TickerHeader";

export default function TickerPage() {
  const symbol = (useParams().symbol ?? "").toUpperCase();
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const ticker = useTicker(symbol);
  const data = ticker.data;
  const job = useLiveTicker(symbol, data);
  const [refreshProblem, setRefreshProblem] = useState<unknown>(null);
  const [refreshing, setRefreshing] = useState(false);
  const height = useMediaHeight();

  const selected = params.get("m") ?? data?.movements[0]?.date ?? null;
  const select = useCallback((date: string) => {
    const next = new URLSearchParams(window.location.search);
    next.set("m", date);
    setParams(next, { replace: true });
  }, [setParams]);
  const movement = data?.movements.find((m) => m.date === selected) ?? null;

  useEffect(() => {
    document.title = data ? `${symbol}: unusual moves explained | Metrix` : `${symbol} | Metrix`;
  }, [data, symbol]);

  const refresh = async () => {
    setRefreshing(true);
    setRefreshProblem(null);
    try {
      client.setQueryData(keys.ticker(symbol), await api.ticker(symbol, { refresh: true }));
    } catch (error) {
      setRefreshProblem(error);
    } finally {
      setRefreshing(false);
    }
  };

  if (ticker.isPending) return <TickerSkeleton />;
  if (ticker.error && !data) {
    const problem = describeError(ticker.error);
    return <div className="grid gap-4"><h1 className="text-title"><span className="mono">{symbol}</span></h1><Notice problem={problem} onRetry={() => void ticker.refetch()} /></div>;
  }
  if (!data) return null;
  const working = isWorking(data);
  const hasData = (data.prices?.length ?? 0) > 0;

  return (
    <div className="grid gap-5">
      <TickerHeader data={data} onRefresh={() => void refresh()} refreshing={refreshing || working} />
      {refreshProblem != null && <Notice problem={describeError(refreshProblem)} />}
      {working && <Progress symbol={symbol} job={job ?? { status: "queued", progress: null }} />}
      {data.warnings.map((w) => <p key={w} className="rounded-control bg-warn-soft px-3 py-2 text-body text-ink">{w}</p>)}
      {hasData ? (
        <>
          <div className="grid gap-5 lg:grid-cols-[minmax(0,8fr)_minmax(0,4fr)]">
            <Panel className="p-4">
              <PriceChart prices={data.prices ?? []} movements={data.movements} benchmarks={data.benchmarks} selected={selected} onSelect={select} height={height} />
            </Panel>
            <Panel className="p-4" as="aside" aria-label="Selected move">
              <SelectedMove symbol={symbol} movement={movement} />
            </Panel>
          </div>
          {data.movements.length ? (
            <MovementsTable symbol={symbol} movements={data.movements} selected={selected} onSelect={select} />
          ) : (
            <Empty title={`No unusual moves in ${symbol}'s history`}>Its daily returns stayed inside its own normal range.</Empty>
          )}
        </>
      ) : working ? (
        <Skeleton className={`${CHART_SKELETON} w-full`} />
      ) : (
        <Empty title="No price history stored">Metrix could not find prices for this symbol.</Empty>
      )}
    </div>
  );
}

// The chart's own heights (useMediaHeight) plus its controls and legend.
const CHART_SKELETON = "h-[320px] sm:h-[380px] lg:h-[440px]";

function TickerSkeleton() {
  return (
    <div aria-busy="true" className="grid gap-5">
      <Skeleton className="h-8 w-80 max-w-full" label="Loading" />
      <div className="grid gap-5 lg:grid-cols-[minmax(0,8fr)_minmax(0,4fr)]">
        <Skeleton className={`${CHART_SKELETON} w-full`} />
        <Skeleton className="h-[320px] w-full" />
      </div>
      {Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-9 w-full" />)}
    </div>
  );
}
