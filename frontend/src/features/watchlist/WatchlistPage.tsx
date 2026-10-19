/**
 * /watchlist: the viewer's lists, optionally weighted, and how each holding
 * moved and contributed over the last sessions. Changes save as they are made.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import { describeError } from "../../api/errors";
import { keys, retryable } from "../../api/queries";
import type { Watchlist, WatchlistIn } from "../../api/types";
import { Button, Change, Empty, Notice, Panel, Skeleton, cx } from "../../design/ui";
import { pp, shortDate } from "../../lib/format";
import { evidenceWord } from "../pulse/evidence";

const WINDOWS = [5, 20, 60];

export default function WatchlistPage() {
  const client = useQueryClient();
  const lists = useQuery({ queryKey: keys.watchlists, queryFn: api.watchlists, retry: retryable });
  const [activeId, setActiveId] = useState<number | null>(null);
  const [sessions, setSessions] = useState(5);
  const active = lists.data?.find((w) => w.id === activeId) ?? lists.data?.[0] ?? null;
  useEffect(() => { document.title = "Watchlist | Metrix"; }, []);

  const save = useMutation({
    mutationFn: ({ id, body }: { id: number | null; body: WatchlistIn }) => (id ? api.replaceWatchlist(id, body) : api.createWatchlist(body)),
    onSuccess: (saved) => {
      setActiveId(saved.id);
      void client.invalidateQueries({ queryKey: keys.watchlists });
      void client.invalidateQueries({ queryKey: ["digest", saved.id] });
    },
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteWatchlist(id),
    onSuccess: () => { setActiveId(null); void client.invalidateQueries({ queryKey: keys.watchlists }); },
  });

  if (lists.isPending) return <Skeleton className="h-64 w-full" label="Loading watchlists" />;
  if (lists.error) return <Notice problem={describeError(lists.error)} onRetry={() => void lists.refetch()} />;

  return (
    <div className="grid gap-6">
      <header className="flex flex-wrap items-center gap-3">
        <h1 className="text-title text-ink">Watchlist</h1>
        {(lists.data?.length ?? 0) > 1 && (
          <select aria-label="Watchlist" value={active?.id ?? ""} onChange={(e) => setActiveId(Number(e.target.value))} className="h-9 rounded-control border border-line bg-surface px-2 text-body">
            {lists.data!.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
          </select>
        )}
        <Button variant="quiet" onClick={() => save.mutate({ id: null, body: { name: `Watchlist ${(lists.data?.length ?? 0) + 1}`, holdings: [] } })}>New list</Button>
        {active && (
          <div className="ml-auto inline-flex rounded-control border border-line p-0.5" role="radiogroup" aria-label="Period">
            {WINDOWS.map((n) => (
              <button key={n} role="radio" aria-checked={sessions === n} onClick={() => setSessions(n)}
                className={cx("h-7 rounded-[5px] px-3 text-body", sessions === n ? "bg-accent text-accent-contrast" : "text-mute hover:text-ink")}>{n} sessions</button>
            ))}
          </div>
        )}
      </header>
      {save.error && <Notice problem={describeError(save.error)} />}
      {active ? (
        <>
          <Editor list={active} saving={save.isPending} onSave={(body) => save.mutate({ id: active.id, body })} onDelete={() => remove.mutate(active.id)} />
          <Digest id={active.id} sessions={sessions} />
        </>
      ) : (
        <Empty title="No watchlist yet">
          <Button variant="primary" className="mt-3" onClick={() => save.mutate({ id: null, body: { name: "My watchlist", holdings: [] } })}>Create a watchlist</Button>
        </Empty>
      )}
    </div>
  );
}

function Editor({ list, onSave, onDelete, saving }: { list: Watchlist; onSave: (body: WatchlistIn) => void; onDelete: () => void; saving: boolean }) {
  const [symbol, setSymbol] = useState("");
  const holdings = list.holdings.map((h) => ({ symbol: h.symbol, weight: list.weighted ? Math.round(h.weight * 1000) / 10 : null }));
  const body = (next: { symbol: string; weight: number | null }[]): WatchlistIn => {
    const weighted = next.some((h) => h.weight != null);
    return { name: list.name, holdings: next.map((h) => ({ symbol: h.symbol, weight: weighted ? h.weight ?? 1 : null })) };
  };
  return (
    <Panel className="p-4">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex-1 text-caption text-mute">
          <span className="sr-only">List name</span>
          <input defaultValue={list.name} key={list.id} onBlur={(e) => e.target.value.trim() && e.target.value !== list.name && onSave({ ...body(holdings), name: e.target.value.trim() })}
            className="h-9 w-full max-w-sm rounded-control border border-transparent bg-transparent px-1 text-heading text-ink hover:border-line focus:border-line-strong" />
        </label>
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); const s = symbol.trim().toUpperCase(); if (s) { onSave(body([...holdings, { symbol: s, weight: null }])); setSymbol(""); } }}>
          <label className="sr-only" htmlFor="add-symbol">Add a ticker</label>
          <input id="add-symbol" value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="Add a ticker, e.g. AMD"
            className="mono h-9 w-48 rounded-control border border-line-strong bg-surface px-2 text-body uppercase" />
          <Button type="submit" disabled={!symbol.trim() || saving}>Add</Button>
        </form>
        <Button variant="quiet" onClick={onDelete} aria-label={`Delete ${list.name}`}><Trash2 size={14} aria-hidden /></Button>
      </div>
      {holdings.length > 0 && (
        <ul className="mt-3 flex flex-wrap gap-2">
          {holdings.map((h) => (
            <li key={h.symbol} className="inline-flex items-center gap-2 rounded-full border border-line px-2.5 py-1 text-body">
              <Link to={`/t/${h.symbol}`} className="mono font-medium hover:underline">{h.symbol}</Link>
              <label className="flex items-center gap-1 text-caption text-mute">Weight
                <input type="number" min={0} step={1} inputMode="decimal" defaultValue={h.weight ?? ""} placeholder="equal" aria-label={`Weight of ${h.symbol}`}
                  onBlur={(e) => {
                    const value = e.target.value === "" ? null : Number(e.target.value);
                    if (value !== h.weight && (value == null || value > 0)) onSave(body(holdings.map((x) => (x.symbol === h.symbol ? { ...x, weight: value } : x))));
                  }}
                  className="num w-14 rounded border border-line bg-surface px-1 text-right text-caption text-ink" />
              </label>
              <button onClick={() => onSave(body(holdings.filter((x) => x.symbol !== h.symbol)))} aria-label={`Remove ${h.symbol}`} className="text-mute hover:text-ink">&times;</button>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-caption text-mute">Leave weights empty for equal weights. Weights are relative: 3 and 1 mean 75% and 25%.</p>
    </Panel>
  );
}

function Digest({ id, sessions }: { id: number; sessions: number }) {
  const digest = useQuery({ queryKey: keys.digest(id, sessions), queryFn: () => api.digest(id, sessions), retry: retryable });
  if (digest.isPending) return <Skeleton className="h-48 w-full" />;
  if (digest.error) return <Notice problem={describeError(digest.error)} onRetry={() => void digest.refetch()} />;
  const d = digest.data;
  if (!d) return null;
  if (!d.holdings.length) return <Empty title="Add tickers to see how they moved" />;
  const extent = Math.max(...d.holdings.map((h) => Math.abs(h.contribution ?? 0)), 0.001) * 1.1;
  return (
    <Panel className="p-4" aria-labelledby="digest">
      <h2 id="digest" className="flex flex-wrap items-baseline gap-x-3 text-body font-semibold text-ink">
        Over {d.sessions.length} sessions{d.sessions.length ? `, ${shortDate(d.sessions[0])} to ${shortDate(d.sessions.at(-1))}` : ""}
        {d.period_return != null && <span className="font-normal text-mute">Watchlist <Change value={d.period_return} /></span>}
      </h2>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-table">
          <thead className="text-caption text-mute">
            <tr className="border-b border-line">
              <th scope="col" className="h-8 text-left font-normal">Holding</th>
              <th scope="col" className="text-right font-normal">Weight</th>
              <th scope="col" className="text-right font-normal">Return</th>
              <th scope="col" className="min-w-[180px] text-left font-normal">Contribution</th>
              <th scope="col" className="text-left font-normal">Largest move</th>
            </tr>
          </thead>
          <tbody>
            {d.holdings.map((h) => {
              const largest = [...h.moves].sort((a, b) => Math.abs(b.daily_return) - Math.abs(a.daily_return))[0];
              const width = h.contribution == null ? 0 : (Math.abs(h.contribution) / extent) * 50;
              return (
                <tr key={h.symbol} className="h-10 border-b border-line last:border-0">
                  <td><Link to={`/t/${h.symbol}`} className="mono font-medium hover:underline">{h.symbol}</Link></td>
                  <td className="num text-right">{h.has_data || h.period_return != null ? `${Math.round(h.weight * 100)}%` : ""}</td>
                  <td className="text-right">{h.period_return == null ? <span className="text-caption text-mute">No prices yet</span> : <Change value={h.period_return} />}</td>
                  <td>
                    {h.contribution != null && (
                      <span className="flex items-center gap-2">
                        <span className="relative h-2 w-24">
                          <span aria-hidden className="absolute bottom-[-3px] top-[-3px] w-px bg-line-strong/50" style={{ left: "50%" }} />
                          <span aria-hidden className={cx("absolute h-full rounded-[2px]", h.contribution >= 0 ? "bg-up/70" : "bg-down/70")} style={h.contribution >= 0 ? { left: "50%", width: `${width}%` } : { right: "50%", width: `${width}%` }} />
                        </span>
                        <span className="num">{pp(h.contribution, 2)}</span>
                      </span>
                    )}
                  </td>
                  <td className="max-w-[320px] truncate">
                    {largest ? (
                      <Link to={`/t/${h.symbol}/m/${largest.date}`} className="hover:underline">
                        {shortDate(largest.date)} <Change value={largest.daily_return} /> <span className="text-mute">{largest.evidence === "explained" ? largest.explanation : evidenceWord(largest.evidence)}</span>
                      </Link>
                    ) : <span className="text-caption text-mute">No flagged moves</span>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {d.missing.length > 0 && (
        <p className="mt-3 text-caption text-mute">
          No prices stored yet for {d.missing.map((s, i) => <span key={s}>{i ? ", " : ""}<Link className="mono underline" to={`/t/${s}`}>{s}</Link></span>)}. Opening a ticker researches it.
        </p>
      )}
    </Panel>
  );
}
