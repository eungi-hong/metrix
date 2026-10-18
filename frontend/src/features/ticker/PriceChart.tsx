/**
 * The price chart: candles (or a line on narrow screens) with a volume pane,
 * movement, filing and earnings markers, optional benchmark overlays rebased
 * to 100, and a band on the selected session. TradingView Lightweight
 * Charts (Apache-2.0); its attribution stays on the chart, as its licence asks.
 */
import {
  CandlestickSeries, ColorType, HistogramSeries, LineSeries, LineStyle, createChart, createSeriesMarkers,
  type IChartApi, type ISeriesApi, type ISeriesMarkersPluginApi, type Time,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import type { BenchmarkSeries, Movement, PriceBar } from "../../api/types";
import { cx } from "../../design/ui";
import { RANGES, RANGE_BARS, buildMarkers, rebase, type Range } from "./markers";

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

function useThemeVersion(): number {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const bump = () => setVersion((v) => v + 1);
    const observer = new MutationObserver(bump);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    media?.addEventListener?.("change", bump);
    return () => { observer.disconnect(); media?.removeEventListener?.("change", bump); };
  }, []);
  return version;
}

export function PriceChart({ prices, movements, benchmarks, selected, onSelect, height }: {
  prices: PriceBar[];
  movements: Movement[];
  benchmarks: BenchmarkSeries[] | null | undefined;
  selected: string | null;
  onSelect: (date: string) => void;
  height: number;
}) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<{ price?: ISeriesApi<"Candlestick"> | ISeriesApi<"Line">; volume?: ISeriesApi<"Histogram">; band?: ISeriesApi<"Histogram">; overlays: ISeriesApi<"Line">[]; markers?: ISeriesMarkersPluginApi<Time> }>({ overlays: [] });
  const [range, setRange] = useState<Range>("1Y");
  const [overlay, setOverlay] = useState<Record<string, boolean>>({});
  const theme = useThemeVersion();
  const narrow = height < 280;
  const dates = useMemo(() => new Set(movements.map((m) => m.date)), [movements]);
  // Read by the chart's click handler, which lives as long as the chart does.
  const latest = useRef({ dates, onSelect });
  latest.current = { dates, onSelect };

  // Build the chart (again when the theme or the shape changes).
  useEffect(() => {
    if (!host.current) return;
    const c = createChart(host.current, {
      autoSize: true,
      // A fixed locale: some browsers report one Intl rejects (en-US@posix), which breaks every label.
      localization: { locale: "en-US" },
      layout: { background: { type: ColorType.Solid, color: css("--surface") }, textColor: css("--text-muted"), fontFamily: "Inter Variable, ui-sans-serif, system-ui", fontSize: 11, attributionLogo: true, panes: { separatorColor: css("--border") } },
      grid: { vertLines: { visible: false }, horzLines: { color: css("--border") } },
      rightPriceScale: { borderVisible: false },
      leftPriceScale: { visible: false, borderVisible: false },
      timeScale: { borderVisible: false, fixLeftEdge: true, fixRightEdge: true },
      crosshair: { horzLine: { labelBackgroundColor: css("--text") }, vertLine: { labelBackgroundColor: css("--text") } },
    });
    chart.current = c;
    const up = css("--up");
    const down = css("--down");
    const price = narrow
      ? c.addSeries(LineSeries, { color: css("--text"), lineWidth: 2, priceLineVisible: false })
      : c.addSeries(CandlestickSeries, { upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down, priceLineVisible: false });
    const band = c.addSeries(HistogramSeries, { priceScaleId: "band", color: css("--text-muted"), priceLineVisible: false, lastValueVisible: false });
    band.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
    const volume = c.addSeries(HistogramSeries, { priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false, color: css("--border-strong") }, 1);
    c.panes()[1]?.setHeight(Math.round(height * 0.2));
    series.current = { price, volume, band, overlays: [], markers: createSeriesMarkers(price, []) };
    c.subscribeClick((param) => {
      const time = typeof param.time === "string" ? param.time : null;
      if (time && latest.current.dates.has(time)) latest.current.onSelect(time);
    });
    return () => { c.remove(); chart.current = null; };
  }, [theme, narrow, height]);

  // Data.
  useEffect(() => {
    const s = series.current;
    if (!s.price || !s.volume) return;
    if (narrow) (s.price as ISeriesApi<"Line">).setData(prices.map((b) => ({ time: b.date, value: b.adj_close })));
    else (s.price as ISeriesApi<"Candlestick">).setData(prices.filter((b) => b.open != null && b.high != null && b.low != null && b.close != null).map((b) => ({ time: b.date, open: b.open!, high: b.high!, low: b.low!, close: b.close! })));
    s.volume.setData(prices.map((b) => ({ time: b.date, value: b.volume ?? 0, color: (b.close ?? 0) >= (b.open ?? 0) ? css("--up") + "55" : css("--down") + "55" })));
  }, [prices, narrow, theme]);

  // Markers and the selected band.
  useEffect(() => {
    const s = series.current;
    s.markers?.setMarkers(buildMarkers(movements, range, { market: css("--c-market"), sector: css("--c-sector"), company: css("--c-company"), neutral: css("--text-muted"), filing: css("--text") }) as never);
    s.band?.setData(selected ? [{ time: selected, value: 1, color: css("--text-muted") + "22" }] : []);
  }, [movements, range, selected, theme]);

  // Overlays.
  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    series.current.overlays.forEach((o) => c.removeSeries(o));
    series.current.overlays = [];
    const shown = (benchmarks ?? []).filter((b) => overlay[b.symbol]);
    c.applyOptions({ leftPriceScale: { visible: shown.length > 0 } });
    for (const b of shown) {
      const line = c.addSeries(LineSeries, { priceScaleId: "left", color: css("--text-muted"), lineWidth: 1, lineStyle: b.role === "sector" ? LineStyle.Dashed : LineStyle.Solid, priceLineVisible: false, lastValueVisible: false, title: b.symbol });
      line.setData(rebase(b.points));
      series.current.overlays.push(line);
    }
  }, [benchmarks, overlay, theme]);

  // Range.
  useEffect(() => {
    const c = chart.current;
    if (!c || !prices.length) return;
    const bars = RANGE_BARS[range];
    if (bars == null) c.timeScale().fitContent();
    else c.timeScale().setVisibleLogicalRange({ from: Math.max(prices.length - bars, 0), to: prices.length });
  }, [range, prices.length, theme, narrow]);

  // [ and ] change the range.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") return;
      const i = RANGES.indexOf(range);
      if (e.key === "[" && i > 0) setRange(RANGES[i - 1]);
      if (e.key === "]" && i < RANGES.length - 1) setRange(RANGES[i + 1]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [range]);

  return (
    <figure className="m-0">
      <div className="mb-2 flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="inline-flex rounded-control border border-line p-0.5" role="radiogroup" aria-label="Chart range">
          {RANGES.map((r) => (
            <button key={r} role="radio" aria-checked={range === r} onClick={() => setRange(r)}
              className={cx("h-7 rounded-[5px] px-2.5 text-caption font-medium", range === r ? "bg-accent text-accent-contrast" : "text-mute hover:text-ink")}>{r}</button>
          ))}
        </div>
        {(benchmarks ?? []).map((b) => (
          <label key={b.symbol} className="flex items-center gap-1.5 text-caption text-mute">
            <input type="checkbox" checked={Boolean(overlay[b.symbol])} onChange={(e) => setOverlay((o) => ({ ...o, [b.symbol]: e.target.checked }))} />
            <span className="mono">{b.symbol}</span> {b.role === "market" ? "market" : "sector"}, rebased to 100
          </label>
        ))}
      </div>
      <div ref={host} style={{ height }} className="w-full" data-testid="price-chart" aria-label="Price chart with flagged moves marked" role="img" />
      <figcaption className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-mute">
        <span><span aria-hidden>{"▲▼"}</span> flagged move, coloured by what drove it:</span>
        <span className="inline-flex items-center gap-1"><i className="inline-block size-2 rounded-sm bg-market" /> market</span>
        <span className="inline-flex items-center gap-1"><i className="inline-block size-2 rounded-sm bg-sector" /> sector</span>
        <span className="inline-flex items-center gap-1"><i className="inline-block size-2 rounded-sm bg-company" /> company</span>
        <span>F filing, E earnings</span>
        <a className="ml-auto underline underline-offset-2" href="https://www.tradingview.com/" target="_blank" rel="noreferrer">Charts by TradingView</a>
      </figcaption>
    </figure>
  );
}
