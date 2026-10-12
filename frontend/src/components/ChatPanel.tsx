import { ArrowUp, ExternalLink, MessageSquareMore, Plus, Sparkles } from "lucide-react";
import { FormEvent, useState } from "react";
import type { ChatMessage } from "../api/types";

const suggestedQuestions = [
  "What market themes matter this week?",
  "What drove NVDA's latest move?",
  "How should I frame the key risks?",
];

export function ChatPanel({ messages, isLoading, ticker, onSend, onNewConversation, onSelectMovement }: {
  messages: ChatMessage[];
  isLoading: boolean;
  ticker?: string;
  onSend: (question: string) => void;
  onNewConversation: () => void;
  onSelectMovement: (movementId: number) => void;
}) {
  const [question, setQuestion] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!question.trim() || isLoading) return;
    onSend(question.trim());
    setQuestion("");
  }

  function askSuggestedQuestion(prompt: string) {
    if (!isLoading) onSend(prompt);
  }

  return <section aria-labelledby="ask-metrix-heading" className="flex min-h-[560px] flex-1 flex-col overflow-hidden rounded-xl border border-line bg-panel shadow-panel">
    <header className="flex items-center justify-between border-b border-line px-5 py-4">
      <div>
        <p className="text-[10px] font-bold uppercase tracking-[0.15em] text-mute">AI research assistant</p>
        <h1 id="ask-metrix-heading" className="mt-1 text-lg font-semibold tracking-tight">Ask Metrix</h1>
      </div>
      <div className="flex items-center gap-2">
        <span className="hidden rounded-lg bg-surface-raised px-2 py-1 text-[11px] font-medium text-mute sm:inline-flex">{ticker ? `${ticker} research` : "General research"}</span>
        <button onClick={onNewConversation} className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel px-2.5 py-1.5 text-xs font-semibold transition-colors hover:bg-surface"><Plus size={13} /> New chat</button>
      </div>
    </header>

    <div className="scrollbar-thin flex-1 overflow-y-auto px-4 py-6 sm:px-8">
      <div className="mx-auto flex min-h-full max-w-3xl flex-col">
        {!messages.length ? <div className="my-auto py-12 text-center">
          <div className="mx-auto grid size-12 place-items-center rounded-2xl bg-accent-soft text-accent"><Sparkles size={22} /></div>
          <h2 className="mt-5 text-2xl font-semibold tracking-tight">What would you like to know?</h2>
          <p className="mx-auto mt-2 max-w-md text-sm leading-6 text-mute">{ticker ? `Ask grounded questions about ${ticker} and its stored research.` : "Ask about markets, companies, or your research. Mention a ticker for company-specific evidence."}</p>
          <div className="mx-auto mt-6 flex max-w-xl flex-wrap justify-center gap-2">
            {suggestedQuestions.map((prompt) => <button key={prompt} type="button" onClick={() => askSuggestedQuestion(prompt)} className="rounded-full border border-line bg-panel px-3 py-1.5 text-xs font-medium text-ink transition-colors hover:border-accent-line hover:bg-accent-soft">{prompt}</button>)}
          </div>
        </div> : <div className="space-y-6 pb-6">
          {messages.map((message) => message.role === "user" ? <div key={message.id} className="flex justify-end"><div className="max-w-[82%] rounded-2xl bg-surface-raised px-4 py-3 text-sm leading-6 text-ink">{message.content}</div></div> : <article key={message.id} className="flex gap-3">
            <span className="grid size-7 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent"><MessageSquareMore size={14} /></span>
            <div className="min-w-0 flex-1 pt-0.5 text-sm leading-6 text-ink">
              <p>{message.content}</p>
              <p className={`mt-3 text-[11px] font-semibold ${message.grounded ? "text-ink" : "text-mute"}`}>{message.grounded ? "Grounded in stored evidence" : "No matching stored evidence"}</p>
              {message.sources && (message.sources.movements.length > 0 || message.sources.articles.length > 0) && <div className="mt-3 flex flex-wrap gap-1.5">
                {message.sources.movements.map((source) => <button key={source.ref} onClick={() => onSelectMovement(source.movement_id)} className="rounded-md bg-accent-soft px-2 py-1 text-[11px] font-semibold text-accent transition-colors hover:bg-accent-line">{source.ref} · {source.date}</button>)}
                {message.sources.articles.map((source) => <a key={source.ref} href={source.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-md bg-surface-raised px-2 py-1 text-[11px] font-semibold text-ink transition-colors hover:bg-line">{source.ref} <ExternalLink size={10} /></a>)}
              </div>}
            </div>
          </article>)}
          {isLoading && <div className="flex gap-3"><span className="grid size-7 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent"><MessageSquareMore size={14} /></span><p className="pt-1 text-sm text-mute"><span className="status-pulse">Reviewing grounded evidence…</span></p></div>}
        </div>}
      </div>
    </div>

    <form onSubmit={submit} className="border-t border-line bg-surface px-4 py-4 sm:px-8">
      <div className="mx-auto max-w-3xl">
        <label className="sr-only" htmlFor="metrix-question">Ask Metrix</label>
        <div className="relative">
          <textarea id="metrix-question" value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={2000} rows={2} placeholder={ticker ? `Ask about ${ticker}…` : "Ask about markets, companies, or research…"} className="block w-full resize-none rounded-2xl border border-line bg-panel px-4 py-3 pr-14 text-sm leading-5 placeholder:text-mute" />
          <button disabled={!question.trim() || isLoading} className="absolute bottom-2 right-2 inline-flex size-9 items-center justify-center rounded-xl bg-accent text-white transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40" aria-label="Send question"><ArrowUp size={17} /></button>
        </div>
        <p className="mt-2 text-center text-[10px] text-mute">Metrix answers from stored research and cited sources. Not investment advice.</p>
      </div>
    </form>
  </section>;
}
