import { ArrowUp, ExternalLink, MessageSquareMore, Plus } from "lucide-react";
import { FormEvent, useState } from "react";
import type { ChatMessage } from "../api/types";

export function ChatPanel({ messages, isLoading, onSend, onNewConversation, onSelectMovement }: {
  messages: ChatMessage[];
  isLoading: boolean;
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
  return <section id="ask-panel" role="tabpanel" aria-labelledby="ask-tab" className="flex min-h-[500px] flex-col">
    <div className="flex items-center justify-between border-b border-line px-5 py-3"><div><p className="text-sm font-semibold">Ask Metrix</p><p className="text-[11px] text-mute">Answers use stored evidence only.</p></div><button onClick={onNewConversation} className="inline-flex items-center gap-1 rounded-lg border border-line bg-white px-2 py-1.5 text-xs font-semibold hover:bg-[#fafafd]"><Plus size={13} /> New</button></div>
    <div className="scrollbar-thin flex-1 space-y-3 overflow-y-auto px-4 py-4">
      {!messages.length && <div className="rounded-xl border border-dashed border-line bg-white px-4 py-5 text-center"><MessageSquareMore aria-hidden="true" className="mx-auto text-accent" size={24} /><p className="mt-2 text-sm font-semibold">Ask about the evidence</p><p className="mt-1 text-xs leading-5 text-mute">For example: “What drove the largest drop?”</p></div>}
      {messages.map((message) => <div key={message.id} className={`rounded-xl px-3 py-2.5 text-sm leading-5 ${message.role === "user" ? "ml-7 bg-accent text-white" : "mr-3 border border-line bg-white text-ink"}`}>
        <p>{message.content}</p>
        {message.role === "assistant" && <>
          <p className={`mt-2 text-[11px] font-semibold ${message.grounded ? "text-emerald-700" : "text-amber-800"}`}>{message.grounded ? "Grounded in stored evidence" : "No matching stored evidence"}</p>
          {message.sources && (message.sources.movements.length > 0 || message.sources.articles.length > 0) && <div className="mt-2 flex flex-wrap gap-1.5">
            {message.sources.movements.map((source) => <button key={source.ref} onClick={() => onSelectMovement(source.movement_id)} className="rounded-md bg-violet-50 px-1.5 py-0.5 text-[11px] font-semibold text-accent hover:bg-violet-100">{source.ref} · {source.date}</button>)}
            {message.sources.articles.map((source) => <a key={source.ref} href={source.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-md bg-[#f4f4f8] px-1.5 py-0.5 text-[11px] font-semibold text-ink hover:bg-[#e9e9f0]">{source.ref} <ExternalLink size={10} /></a>)}
          </div>}
        </>}
      </div>)}
      {isLoading && <div className="mr-16 rounded-xl border border-line bg-white px-3 py-2.5 text-xs text-mute"><span className="status-pulse">Finding grounded evidence…</span></div>}
    </div>
    <form onSubmit={submit} className="border-t border-line bg-[#fcfcfe] p-3">
      <label className="sr-only" htmlFor="metrix-question">Ask a question</label>
      <textarea id="metrix-question" value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={2000} rows={2} placeholder="Ask about a move or source…" className="block w-full resize-none rounded-xl border border-line bg-white px-3 py-2 text-sm placeholder:text-mute" />
      <div className="mt-2 flex items-center justify-between"><p className="text-[10px] text-mute">Grounded answers · not investment advice</p><button disabled={!question.trim() || isLoading} className="inline-flex size-8 items-center justify-center rounded-lg bg-accent text-white hover:bg-[#4b45c4] disabled:cursor-not-allowed disabled:opacity-40" aria-label="Send question"><ArrowUp size={16} /></button></div>
    </form>
  </section>;
}
