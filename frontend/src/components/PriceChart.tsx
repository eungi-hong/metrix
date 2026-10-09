import { ArrowDownRight, ArrowUpRight, ChartNoAxesCombined, ChevronDown, Database, Info } from "lucide-react";
import { useMemo, useState } from "react";
import { Line, LineChart, ReferenceDot, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Movement, PriceBar, TickerDetail } from "../api/types";
import { currency, pct, shortDate } from "./format";

type Range = "1M" | "3M" | "6M" | "All";
const ranges: Range[] = ["1M", "3M", "6M", "All"];

function filteredPrices(prices: PriceBar[], range: Range) {
  if (range === "All") return prices;
  const days = range === "1M" ? 31 : range === "3M" ? 93 : 186;
  const end = new Date(`${prices.at(-1)?.date ?? ""}T12:00:00Z`).getTime();
  return prices.filter((bar) => end - new Date(`${bar.date}T12:00:00Z`).getTime() <= days * 86_400_000);
}

function MovementMarker({ cx, cy, movement, selected, onSelect }: { cx?: number; cy?: number; movement: Movement; selected: boolean; onSelect: () => void }) {
  if (cx == null || cy == null) return null;
  const up = movement.direction === "up";
  const color = up ? "#16815c" : "#c53b48";
  const path = up ? "M0,-7 L6,4 L-6,4 Z" : "M0,7 L6,-4 L-6,-4 Z";
  return <g transform={`translate(${cx},${cy})`} role="button" tabIndex={0} aria-label={`${up ? "Up" : "Down"} movement ${pct(movement.daily_return_pct, true)} on ${shortDate(movement.date)}`} onClick={onSelect} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(); } }} className="cursor-pointer">
    {selected && <circle r="12" fill={color} opacity="0.14" />}
    <path d={path} fill={color} stroke="white" strokeWidth="1.5" />
  </g>;
}

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  const row = payload[0].payload as PriceBar;
  return <div className="rounded-lg border border-line bg-white px-3 py-2 shadow-lg"><p className="text-[11px] text-mute">{shortDate(label)}</p><p className="mt-0.5 text-sm font-semibold">{currency(row.adj_close)}</p>{row.movement && <p className={`mt-1 text-xs font-semibold ${row.movement.direction === "up" ? "text-emerald-700" : "text-red-700"}`}>{row.movement.direction === "up" ? "▲" : "▼"} {pct(row.movement.daily_return_pct, true)} movement</p>}</div>;
}

export function PriceChart({ data, selectedMovement, onSelectMovement }: { data: TickerDetail | null; selectedMovement: Movement | null; onSelectMovement: (movement: Movement) => void }) {
  const [range, setRange] = useState<Range>("All");
  const prices = data?.prices ?? [];
  const visiblePrices = useMemo(() => filteredPrices(prices, range), [prices, range]);
  const visibleMoves = useMemo(() => (data?.movements ?? []).filter((move) => visiblePrices.some((bar) => bar.date === move.date)), [data?.movements, visiblePrices]);
  const chartData = useMemo(() => {
    const movementByDate = new Map((data?.movements ?? []).map((movement) => [movement.date, movement]));
    return visiblePrices.map((bar) => ({ ...bar, movement: movementByDate.get(bar.date) }));
  }, [data?.movements, visiblePrices]);
  const currencyCode = data?.ticker.currency ?? "USD";
  const rangeText = data?.price_range.start && data.price_range.end ? `${shortDate(data.price_range.start)} – ${shortDate(data.price_range.end)}` : "No stored range";

  return <div className="flex h-full min-h-[550px] flex-col rounded-2xl border border-line bg-white p-4 shadow-panel sm:p-5">
    <div className="flex flex-col justify-between gap-4 border-b border-line pb-4 sm:flex-row sm:items-start">
      <div><p className="text-[11px] font-bold uppercase tracking-[0.15em] text-mute">Stored historical prices</p><div className="mt-1 flex items-baseline gap-2"><h2 className="text-xl font-semibold">{data?.ticker.symbol ?? "NVDA"}</h2><span className="text-sm text-mute">Adjusted close</span></div><p className="mt-1 text-xs text-mute">{rangeText}{data?.price_range.bars ? ` · ${data.price_range.bars} daily bars` : ""}</p></div>
      <div className="flex items-center gap-1 rounded-xl border border-line bg-[#fafafd] p-1" aria-label="Chart date range">{ranges.map((option) => <button key={option} onClick={() => setRange(option)} className={`rounded-lg px-2.5 py-1.5 text-xs font-semibold ${range === option ? "bg-white text-ink shadow-sm" : "text-mute hover:text-ink"}`}>{option}</button>)}</div>
    </div>
    {selectedMovement && <div className="mt-4 flex items-center gap-2 rounded-xl border border-violet-100 bg-violet-50 px-3 py-2 text-xs text-violet-950"><span className={`grid size-6 place-items-center rounded-full ${selectedMovement.direction === "up" ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-700"}`}>{selectedMovement.direction === "up" ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}</span><p><span className="font-semibold">Selected · {shortDate(selectedMovement.date)}</span> — {pct(selectedMovement.daily_return_pct, true)} ({selectedMovement.sigma_multiple?.toFixed(1) ?? "—"}σ)</p></div>}
    {visiblePrices.length ? <>
      <div className="min-h-[360px] flex-1 pt-5" role="img" aria-label={`Adjusted close chart for ${data?.ticker.symbol ?? "selected ticker"}; ${visibleMoves.length} movement markers are keyboard accessible.`}>
        <ResponsiveContainer width="100%" height="100%"><LineChart data={chartData} margin={{ top: 22, right: 14, bottom: 8, left: 2 }}>
          <XAxis dataKey="date" tickFormatter={(value) => new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(new Date(`${value}T12:00:00Z`))} interval="preserveStartEnd" minTickGap={54} axisLine={false} tickLine={false} tick={{ fill: "#747389", fontSize: 11 }} />
          <YAxis dataKey="adj_close" domain={["dataMin - 4", "dataMax + 4"]} tickFormatter={(value) => `$${value.toFixed(0)}`} width={48} axisLine={false} tickLine={false} tick={{ fill: "#747389", fontSize: 11 }} />
          <Tooltip content={<ChartTooltip />} cursor={{ stroke: "#d6d5ee", strokeWidth: 1 }} />
          <Line type="monotone" dataKey="adj_close" stroke="#5b55d9" strokeWidth={2.25} dot={false} activeDot={{ r: 4, fill: "#5b55d9", stroke: "white", strokeWidth: 2 }} />
          {visibleMoves.map((movement) => <ReferenceDot key={movement.id} x={movement.date} y={movement.adj_close} shape={(props: any) => <MovementMarker {...props} movement={movement} selected={selectedMovement?.id === movement.id} onSelect={() => onSelectMovement(movement)} />} />)}
        </LineChart></ResponsiveContainer>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3 text-[11px] text-mute"><span className="inline-flex items-center gap-1"><Database size={12} /> Stored daily data; no live or intraday pricing.</span><span className="inline-flex items-center gap-1"><span className="text-emerald-700">▲</span> Up move <span className="ml-1 text-red-700">▼</span> Down move</span></div>
      <p className="sr-only">Visible range from {visiblePrices[0]?.date} to {visiblePrices.at(-1)?.date}. Latest adjusted close: {currency(visiblePrices.at(-1)?.adj_close, currencyCode)}.</p>
    </> : <div className="flex flex-1 flex-col items-center justify-center px-6 text-center"><div className="grid size-12 place-items-center rounded-2xl bg-violet-50 text-accent"><ChartNoAxesCombined size={23} /></div><h3 className="mt-4 text-base font-semibold">Price history will appear here</h3><p className="mt-1 max-w-sm text-sm leading-6 text-mute">Metrix only charts stored daily bars. Start an analysis or wait for the queued job—no live pricing is fabricated.</p><div className="mt-4 inline-flex items-center gap-1 text-xs text-mute"><Info size={13} /> The chart follows the API’s <code className="ml-1 rounded bg-[#f4f4f8] px-1">include_prices=true</code> data.</div></div>}
  </div>;
}
