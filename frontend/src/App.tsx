import { BarChart3 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, MetrixApiClient } from "./api/client";
import type { ChatMessage, Job, Movement, TickerDetail } from "./api/types";
import { AppShell } from "./components/AppShell";
import { ApiKeySettings } from "./components/ApiKeySettings";
import { ChatPanel } from "./components/ChatPanel";
import { DemoDataBanner } from "./components/DemoDataBanner";
import { ErrorState } from "./components/ErrorState";
import { JobProgress } from "./components/JobProgress";
import { MovementsPanel } from "./components/MovementsPanel";
import { NewsPanel } from "./components/NewsPanel";
import { OverviewPanel } from "./components/OverviewPanel";
import { PriceChart } from "./components/PriceChart";
import { ResearchTabs, type ResearchTab } from "./components/ResearchTabs";
import { TickerHeader } from "./components/TickerHeader";
import { TickerSearch } from "./components/TickerSearch";
import { demoChatResponse, readyDemo } from "./fixtures/demo";
import { phaseForTicker } from "./state/ticker";

const API_BASE_URL = import.meta.env.VITE_METRIX_API_BASE_URL ?? "http://localhost:8000";
const KEY_STORAGE = "metrix-api-key";

function readSessionKey() {
  try { return sessionStorage.getItem(KEY_STORAGE) ?? ""; } catch { return ""; }
}

function sourceMessage(data: TickerDetail | null, isDemo: boolean) {
  if (!data) return null;
  return isDemo ? "Viewing local fixture data" : "Connected to local Metrix API";
}

export default function App() {
  const [apiKey, setApiKey] = useState(readSessionKey);
  const [mode, setMode] = useState<"none" | "api" | "demo">("none");
  const [symbol, setSymbol] = useState("NVDA");
  const [data, setData] = useState<TickerDetail | null>(null);
  const [activeTab, setActiveTab] = useState<ResearchTab>("overview");
  const [selectedMovementId, setSelectedMovementId] = useState<number | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<ApiError | Error | null>(null);
  const [loading, setLoading] = useState(false);
  const [chatLoading, setChatLoading] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | undefined>();

  const selectedMovement = useMemo(() => data?.movements.find((movement) => movement.id === selectedMovementId) ?? null, [data, selectedMovementId]);
  const phase = phaseForTicker(data, loading);

  const fetchApiTicker = useCallback(async (nextSymbol: string, refresh = false) => {
    if (!apiKey) {
      setError(new ApiError("Enter an mtx_ API key in settings, or choose demo data for a local preview.", "credentials", 401));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await new MetrixApiClient(API_BASE_URL, apiKey).getTicker(nextSymbol, { refresh });
      setData(result.data);
      setMode("api");
      setJob(result.data.job_id ? { id: result.data.job_id, kind: "ingest_ticker", status: "queued", source: "interactive", priority: 0, payload: {}, progress: null, attempts: 0, max_attempts: 3, last_error: null, run_after: "", created_at: "", locked_at: null, finished_at: null } : null);
      setSelectedMovementId(result.data.movements[0]?.id ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason : new Error("Unable to load research."));
    } finally {
      setLoading(false);
    }
  }, [apiKey]);

  const analyze = useCallback((nextSymbol: string, refresh = false) => {
    const normalized = nextSymbol.toUpperCase();
    setSymbol(normalized);
    setMessages([]);
    setConversationId(undefined);
    if (mode === "demo" && !apiKey) {
      setError(null);
      setData({ ...readyDemo, ticker: { ...readyDemo.ticker, symbol: normalized, company_name: normalized === "NVDA" ? readyDemo.ticker.company_name : `${normalized} demo research` } });
      setSelectedMovementId(readyDemo.movements[0]?.id ?? null);
      return;
    }
    void fetchApiTicker(normalized, refresh);
  }, [apiKey, fetchApiTicker, mode]);

  useEffect(() => {
    if (!job || mode !== "api" || !apiKey) return;
    let cancelled = false;
    const client = new MetrixApiClient(API_BASE_URL, apiKey);
    const poll = async () => {
      try {
        const latest = await client.getJob(job.id);
        if (cancelled) return;
        setJob(latest);
        if (latest.status === "succeeded") {
          setJob(null);
          void fetchApiTicker(symbol);
        }
        if (latest.status === "dead") {
          setJob(null);
          setError(new ApiError(latest.last_error ?? "The queued analysis did not complete.", "upstream", 502));
        }
      } catch (reason) {
        if (!cancelled) setError(reason instanceof Error ? reason : new Error("Unable to check job progress."));
      }
    };
    void poll();
    const id = window.setInterval(() => void poll(), 3500);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [apiKey, fetchApiTicker, job, mode, symbol]);

  function useDemo() {
    setMode("demo");
    setSymbol("NVDA");
    setData(readyDemo);
    setSelectedMovementId(readyDemo.movements[0]?.id ?? null);
    setError(null);
    setJob(null);
  }

  function selectMovement(movement: Movement) {
    setSelectedMovementId(movement.id);
    setActiveTab("news");
  }

  async function sendQuestion(question: string) {
    if (!data) return;
    const userMessage: ChatMessage = { id: `q-${Date.now()}`, role: "user", content: question };
    setMessages((previous) => [...previous, userMessage]);
    setChatLoading(true);
    try {
      const response = mode === "demo"
        ? demoChatResponse
        : await new MetrixApiClient(API_BASE_URL, apiKey).sendChat({ ticker: data.ticker.symbol, question, conversationId });
      setConversationId(response.conversation_id);
      setMessages((previous) => [...previous, { id: `a-${Date.now()}`, role: "assistant", content: response.answer, sources: response.sources, grounded: response.grounded }]);
    } catch (reason) {
      setError(reason instanceof Error ? reason : new Error("Unable to answer that question."));
    } finally {
      setChatLoading(false);
    }
  }

  function saveKey(key: string, persist: boolean) {
    setApiKey(key);
    setMode(key ? "api" : mode);
    try {
      if (persist && key) sessionStorage.setItem(KEY_STORAGE, key);
      else sessionStorage.removeItem(KEY_STORAGE);
    } catch { /* Browsers may deny session storage; in-memory key still works. */ }
  }

  function clearKey() {
    setApiKey("");
    try { sessionStorage.removeItem(KEY_STORAGE); } catch { /* no storage available */ }
  }

  const research = <div className="flex min-h-full flex-col">
    <div className="flex items-center justify-between px-5 pb-3 pt-5"><div className="flex items-center gap-2"><span className="grid size-8 place-items-center rounded-lg bg-accent text-white"><BarChart3 size={17} /></span><span className="text-base font-semibold tracking-tight">metrix</span></div><ApiKeySettings apiKey={apiKey} onSave={saveKey} onClear={clearKey} /></div>
    <div className="px-5 pb-4"><TickerSearch initialSymbol={symbol} onAnalyze={analyze} disabled={loading} /></div>
    {data ? <>
      <TickerHeader data={data} onRefresh={() => analyze(symbol, true)} refreshing={loading || phase === "processing"} />
      <ResearchTabs active={activeTab} onChange={setActiveTab} />
      <JobProgress data={data} job={job} />
      {error && <ErrorState error={error} onRetry={() => analyze(symbol)} />}
      {activeTab === "overview" && <OverviewPanel data={data} />}
      {activeTab === "movements" && <MovementsPanel movements={data.movements} selectedId={selectedMovementId} onSelect={selectMovement} />}
      {activeTab === "news" && <NewsPanel movement={selectedMovement} />}
      {activeTab === "ask" && <ChatPanel messages={messages} isLoading={chatLoading} onSend={sendQuestion} onNewConversation={() => { setMessages([]); setConversationId(undefined); }} onSelectMovement={(movementId) => { const movement = data.movements.find((item) => item.id === movementId); if (movement) selectMovement(movement); }} />}
    </> : <>
      {error ? <ErrorState error={error} onRetry={() => analyze(symbol)} /> : <DemoDataBanner onUseDemo={useDemo} />}
      <div className="px-5 py-6"><p className="text-sm font-semibold">Start with a stored asset</p><p className="mt-1 text-sm leading-6 text-mute">Search a ticker with your local API key, or use the explicitly labelled NVDA fixture to explore the research workspace.</p></div>
    </>}
    {sourceMessage(data, mode === "demo") && <p className="mt-auto border-t border-line px-5 py-3 text-[11px] text-mute">{sourceMessage(data, mode === "demo")}</p>}
  </div>;

  return <AppShell research={research} chart={<PriceChart data={data} selectedMovement={selectedMovement} onSelectMovement={selectMovement} />} />;
}
