/**
 * /ask: questions answered from the database, streamed, with every claim
 * cited. Past conversations on the left (wide screens); the composer keeps
 * an optional ticker as context.
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Plus, Square, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "../../api/client";
import { describeError } from "../../api/errors";
import { keys, retryable } from "../../api/queries";
import { streamChat } from "../../api/stream";
import type { ChatResponse, ChatSources } from "../../api/types";
import { Button, Notice, cx } from "../../design/ui";
import { relativeAge } from "../../lib/format";
import { Answer, SourceCards } from "./Answer";
import { activity, rowsFromSummary } from "./citations";

interface Turn {
  id: string;
  question: string;
  answer: string;
  final: boolean;
  sources: ChatSources | null;
  calls: { name: string; summary: string; rows: number }[];
  response: ChatResponse | null;
  error: unknown;
}

const FALLBACK_NOTE = "Summary from stored data. Not written by the model.";

export default function AskPage() {
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<string | undefined>(params.get("c") ?? undefined);
  const [ticker, setTicker] = useState<string | null>(params.get("ticker")?.toUpperCase() ?? null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const autoAsked = useRef(false);
  const end = useRef<HTMLDivElement>(null);

  const conversations = useQuery({ queryKey: keys.conversations, queryFn: api.conversations, retry: retryable, staleTime: 30_000 });
  const loaded = useQuery({ queryKey: keys.conversation(conversationId ?? ""), queryFn: () => api.conversation(conversationId!), enabled: Boolean(conversationId) && turns.length === 0, retry: retryable });

  useEffect(() => { document.title = "Ask Metrix | Metrix"; }, []);

  // A past conversation, opened from the list.
  useEffect(() => {
    if (!loaded.data || turns.length) return;
    const restored: Turn[] = [];
    const messages = loaded.data.messages;
    for (let i = 0; i < messages.length; i += 1) {
      if (messages[i].role !== "user") continue;
      const answer = messages[i + 1]?.role === "assistant" ? messages[i + 1] : null;
      restored.push({ id: `h-${messages[i].id}`, question: messages[i].content, answer: answer?.content ?? "", final: true, sources: answer?.sources ?? null, calls: [], response: null, error: null });
    }
    setTurns(restored);
    if (loaded.data.ticker) setTicker(loaded.data.ticker);
  }, [loaded.data, turns.length]);

  const ask = async (question: string) => {
    const text = question.trim();
    if (!text || busy) return;
    const id = `t-${Date.now()}`;
    const update = (patch: Partial<Turn> | ((t: Turn) => Partial<Turn>)) =>
      setTurns((all) => all.map((t) => (t.id === id ? { ...t, ...(typeof patch === "function" ? patch(t) : patch) } : t)));
    setTurns((all) => [...all, { id, question: text, answer: "", final: false, sources: null, calls: [], response: null, error: null }]);
    setDraft("");
    setBusy(true);
    abort.current = new AbortController();
    try {
      const response = await streamChat(
        { question: text, ticker: ticker ?? undefined, conversation_id: conversationId },
        {
          onDelta: (delta) => update((t) => ({ answer: t.answer + delta })),
          onToolCall: () => update({ answer: "" }), // text before a lookup was thinking aloud
          onToolResult: (name, summary, rows) => update((t) => ({ calls: [...t.calls, { name, summary, rows }] })),
        },
        abort.current.signal,
      );
      update({ answer: response.answer, final: true, sources: response.sources, response, calls: response.tool_calls.map((c) => ({ name: c.name, summary: c.summary, rows: rowsFromSummary(c) })) });
      setConversationId(response.conversation_id);
      const next = new URLSearchParams(params);
      next.set("c", response.conversation_id);
      next.delete("q");
      setParams(next, { replace: true });
      void client.invalidateQueries({ queryKey: keys.conversations });
    } catch (error) {
      if ((error as Error).name === "AbortError") update((t) => ({ final: true, answer: t.answer || "Stopped." }));
      else update({ error, final: true });
    } finally {
      setBusy(false);
      abort.current = null;
    }
  };

  // Arriving with ?q= (from a suggestion or the palette) asks it once.
  useEffect(() => {
    const q = params.get("q");
    if (q && !autoAsked.current && !params.get("c")) {
      autoAsked.current = true;
      void ask(q);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { end.current?.scrollIntoView({ block: "end", behavior: "auto" }); }, [turns]);

  const startNew = () => {
    abort.current?.abort();
    setTurns([]);
    setConversationId(undefined);
    setParams(ticker ? { ticker } : {}, { replace: true });
  };
  const open = (id: string) => {
    abort.current?.abort();
    setTurns([]);
    setConversationId(id);
    setParams({ c: id }, { replace: true });
  };

  return (
    <div className="grid gap-6 lg:grid-cols-[240px_minmax(0,720px)]">
      <aside className="hidden lg:block" aria-label="Past conversations">
        <Button onClick={startNew} className="w-full justify-start"><Plus size={15} aria-hidden /> New question</Button>
        <ul className="mt-4 grid gap-0.5">
          {(conversations.data?.conversations ?? []).map((c) => (
            <li key={c.id}>
              <button onClick={() => open(c.id)} className={cx("w-full rounded-control px-2 py-1.5 text-left text-body", c.id === conversationId ? "bg-surface-2 text-ink" : "text-mute hover:text-ink")}>
                <span className="block truncate">{c.ticker ? <span className="mono mr-1">{c.ticker}</span> : null}{c.messages} messages</span>
                <span className="text-caption text-mute">{relativeAge(c.last_message_at ?? c.created_at)}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section aria-label="Conversation" className="flex min-h-[60vh] flex-col">
        <h1 className="sr-only">Ask Metrix</h1>
        {turns.length === 0 && !loaded.isFetching && <Intro onAsk={(q) => void ask(q)} />}
        <ol className="grid gap-8">
          {turns.map((t) => <TurnView key={t.id} turn={t} onAsk={(q) => void ask(q)} />)}
        </ol>
        <div ref={end} />
        <form className="sticky bottom-16 mt-8 md:bottom-4" onSubmit={(e) => { e.preventDefault(); void ask(draft); }}>
          <div className="rounded-card border border-line-strong bg-surface p-2 shadow-pop">
            {ticker && (
              <span className="mb-1 inline-flex items-center gap-1 rounded-full bg-surface-2 px-2 py-0.5 text-caption text-ink">
                About <span className="mono font-medium">{ticker}</span>
                <button type="button" onClick={() => setTicker(null)} aria-label={`Stop limiting questions to ${ticker}`} className="text-mute hover:text-ink"><X size={12} /></button>
              </span>
            )}
            <div className="flex items-end gap-2">
              <label className="sr-only" htmlFor="question">Your question</label>
              <textarea id="question" rows={2} value={draft} onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void ask(draft); } }}
                placeholder={ticker ? `Ask about ${ticker}` : "Why did NVDA fall in June? Did AMD fall for the same reason?"}
                className="max-h-40 min-h-[44px] flex-1 resize-none bg-transparent px-1 py-1 text-body-lg text-ink outline-none placeholder:text-mute" />
              {busy
                ? <Button type="button" onClick={() => abort.current?.abort()} aria-label="Stop the answer"><Square size={14} aria-hidden /> Stop</Button>
                : <Button type="submit" variant="primary" disabled={!draft.trim()}>Ask</Button>}
            </div>
            <p className="px-1 text-caption text-mute">Enter to send, Shift+Enter for a new line. Answers come only from stored data.</p>
          </div>
        </form>
      </section>
    </div>
  );
}

function Intro({ onAsk }: { onAsk: (q: string) => void }) {
  const examples = ["What were NVDA's largest moves this year, and why?", "Did AMD and NVDA fall for the same reason?", "Which TSLA moves were sector-wide?"];
  return (
    <div className="mb-6">
      <p className="text-title text-ink">Ask about any move Metrix has researched.</p>
      <p className="mt-1 text-body text-mute">Answers are built from stored moves, articles and SEC filings, and every claim links to its source.</p>
      <div className="mt-4 flex flex-wrap gap-2">
        {examples.map((q) => <button key={q} onClick={() => onAsk(q)} className="rounded-full border border-line bg-surface px-3 py-1 text-body text-ink hover:bg-surface-2">{q}</button>)}
      </div>
    </div>
  );
}

function TurnView({ turn, onAsk }: { turn: Turn; onAsk: (q: string) => void }) {
  const [openCalls, setOpenCalls] = useState(false);
  const line = activity(turn.calls);
  const response = turn.response;
  return (
    <li className="grid gap-3">
      <p className="justify-self-end rounded-card bg-surface-2 px-3 py-2 text-body-lg text-ink">{turn.question}</p>
      <div aria-live={turn.final ? "off" : "polite"} aria-busy={!turn.final}>
        {line && (
          <button onClick={() => setOpenCalls((v) => !v)} aria-expanded={openCalls} className="mb-2 inline-flex items-center gap-1 text-caption text-mute hover:text-ink">
            {line} <ChevronDown size={12} aria-hidden className={openCalls ? "rotate-180" : ""} />
          </button>
        )}
        {openCalls && <ul className="mb-2 grid gap-0.5 text-caption text-mute">{turn.calls.map((c, i) => <li key={i}>{c.summary}</li>)}</ul>}
        {response?.answered_by === "fallback" && <p className="mb-2 inline-block rounded-control bg-warn-soft px-2 py-1 text-caption text-ink">{FALLBACK_NOTE}</p>}
        {turn.error ? (
          <Notice problem={describeError(turn.error)} onRetry={() => onAsk(turn.question)} />
        ) : turn.answer ? (
          <Answer text={response?.answered_by === "fallback" ? turn.answer.replace(/^Summary from stored data\.[^\n]*\n?/, "") : turn.answer} sources={turn.sources} final={turn.final} />
        ) : (
          <p className="text-body text-mute">{turn.calls.length ? "Writing the answer" : "Looking it up"}</p>
        )}
        {turn.final && turn.sources && <SourceCards sources={turn.sources} />}
        {response && response.suggestions.length > 0 && (
          <div className="mt-4 flex flex-wrap gap-2" aria-label="Follow-up questions">
            {response.suggestions.map((q) => <button key={q} onClick={() => onAsk(q)} className="rounded-full border border-line bg-surface px-3 py-1 text-body text-ink hover:bg-surface-2">{q}</button>)}
          </div>
        )}
        {response?.ticker && <p className="mt-3 text-caption text-mute">See all of <Link className="mono underline" to={`/t/${response.ticker}`}>{response.ticker}</Link>'s moves.</p>}
      </div>
    </li>
  );
}
