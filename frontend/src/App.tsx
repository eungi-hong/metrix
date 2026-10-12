import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, MetrixApiClient, type ProviderKeys } from "./api/client";
import type { ChatMessage, Job, Movement, TickerDetail } from "./api/types";
import { AppNavigation, type WorkspacePage } from "./components/AppNavigation";
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
const PROVIDER_KEY_STORAGE = "metrix-provider-keys";

function readSessionProviderKeys(): ProviderKeys | null {
  try {
    const stored: unknown = JSON.parse(sessionStorage.getItem(PROVIDER_KEY_STORAGE) ?? "null");
    if (typeof stored !== "object" || stored === null) return null;
    const keys = stored as Partial<ProviderKeys>;
    return typeof keys.exaApiKey === "string" && typeof keys.openaiApiKey === "string"
      && keys.exaApiKey && keys.openaiApiKey
      ? { exaApiKey: keys.exaApiKey, openaiApiKey: keys.openaiApiKey }
      : null;
  } catch { return null; }
}

function sourceMessage(data: TickerDetail | null, isDemo: boolean, connection: "none" | "demo" | "own") {
  if (!data) return null;
  if (isDemo) return "Viewing local fixture data";
  return connection === "own" ? "Live research using your own provider keys" : "Live research using demo access";
}

export default function App() {
  const [providerKeys, setProviderKeys] = useState<ProviderKeys | null>(readSessionProviderKeys);
  const [connection, setConnection] = useState<"none" | "demo" | "own">(
    () => readSessionProviderKeys() ? "own" : "none",
  );
  const [mode, setMode] = useState<"none" | "api" | "demo">("none");
  const [symbol, setSymbol] = useState("NVDA");
  const [data, setData] = useState<TickerDetail | null>(null);
  const [page, setPage] = useState<WorkspacePage>("home");
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
  const jobId = job?.id;

  const fetchApiTicker = useCallback(async (nextSymbol: string, refresh = false) => {
    if (connection === "none") {
      setError(new ApiError("Choose demo access or add both your Exa and OpenAI API keys before running live research.", "credentials", 401));
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await new MetrixApiClient(API_BASE_URL, connection === "own" ? providerKeys ?? undefined : undefined).getTicker(nextSymbol, { refresh, wait: connection === "own" });
      setData(result.data);
      setMode("api");
      setJob(result.data.job_id ? { id: result.data.job_id, kind: "ingest_ticker", status: "queued", source: "interactive", priority: 0, payload: {}, progress: null, attempts: 0, max_attempts: 3, last_error: null, run_after: "", created_at: "", locked_at: null, finished_at: null } : null);
      setSelectedMovementId(result.data.movements[0]?.id ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason : new Error("Unable to load research."));
    } finally {
      setLoading(false);
    }
  }, [connection, providerKeys]);

  const analyze = useCallback((nextSymbol: string, refresh = false) => {
    const normalized = nextSymbol.toUpperCase();
    setSymbol(normalized);
    setMessages([]);
    setConversationId(undefined);
    if (mode === "demo" && connection === "none") {
      setError(null);
      setData({ ...readyDemo, ticker: { ...readyDemo.ticker, symbol: normalized, company_name: normalized === "NVDA" ? readyDemo.ticker.company_name : `${normalized} demo research` } });
      setSelectedMovementId(readyDemo.movements[0]?.id ?? null);
      return;
    }
    void fetchApiTicker(normalized, refresh);
  }, [connection, fetchApiTicker, mode]);

  useEffect(() => {
    if (!jobId || mode !== "api" || connection !== "demo") return;
    let cancelled = false;
    const client = new MetrixApiClient(API_BASE_URL);
    const poll = async () => {
      try {
        const latest = await client.getJob(jobId);
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
  }, [connection, fetchApiTicker, jobId, mode, symbol]);

  function useDemo() {
    setMode("demo");
    setSymbol("NVDA");
    setData(readyDemo);
    setSelectedMovementId(readyDemo.movements[0]?.id ?? null);
    setError(null);
    setJob(null);
    setConnection("none");
  }

  function selectMovement(movement: Movement) {
    setSelectedMovementId(movement.id);
    setPage("home");
    setActiveTab("news");
  }

  function navigate(page: WorkspacePage) {
    setPage(page);
    if (page === "home") setActiveTab("overview");
  }

  async function sendQuestion(question: string) {
    if (mode !== "demo" && connection === "none") {
      setError(new ApiError("Choose demo access or add both your Exa and OpenAI API keys before asking live questions.", "credentials", 401));
      return;
    }
    const userMessage: ChatMessage = { id: `q-${Date.now()}`, role: "user", content: question };
    setMessages((previous) => [...previous, userMessage]);
    setChatLoading(true);
    try {
      const response = mode === "demo"
        ? demoChatResponse
        : await new MetrixApiClient(API_BASE_URL, connection === "own" ? providerKeys ?? undefined : undefined).sendChat({ ticker: data?.ticker.symbol, question, conversationId });
      setConversationId(response.conversation_id);
      setMessages((previous) => [...previous, { id: `a-${Date.now()}`, role: "assistant", content: response.answer, sources: response.sources, grounded: response.grounded }]);
    } catch (reason) {
      setError(reason instanceof Error ? reason : new Error("Unable to answer that question."));
    } finally {
      setChatLoading(false);
    }
  }

  function saveProviderKeys(keys: ProviderKeys, persist: boolean) {
    setProviderKeys(keys);
    setConnection("own");
    try {
      if (persist) sessionStorage.setItem(PROVIDER_KEY_STORAGE, JSON.stringify(keys));
      else sessionStorage.removeItem(PROVIDER_KEY_STORAGE);
    } catch { /* Browsers may deny session storage; in-memory key still works. */ }
  }

  function useDemoAccess() {
    setConnection("demo");
    setMode("none");
    setError(null);
  }

  function clearConnection() {
    setProviderKeys(null);
    setConnection("none");
    try { sessionStorage.removeItem(PROVIDER_KEY_STORAGE); } catch { /* no storage available */ }
  }

  const research = <div className="flex min-h-full flex-col">
    <div id="research-workspace" className="flex items-center justify-between px-5 pb-3 pt-5"><div><p className="text-[10px] font-bold uppercase tracking-[0.15em] text-mute">Research workspace</p><h1 className="mt-1 text-base font-semibold tracking-tight">Good afternoon</h1></div><ApiKeySettings providerKeys={providerKeys} connected={connection} onSave={saveProviderKeys} onUseDemo={useDemoAccess} onClear={clearConnection} /></div>
    <div className="px-5 pb-4"><TickerSearch initialSymbol={symbol} onAnalyze={analyze} disabled={loading} /></div>
    {data ? <>
      <TickerHeader data={data} onRefresh={() => analyze(symbol, true)} refreshing={loading || phase === "processing"} />
      <ResearchTabs active={activeTab} onChange={setActiveTab} />
      <JobProgress data={data} job={job} />
      {error && <ErrorState error={error} onRetry={() => analyze(symbol)} />}
      {activeTab === "overview" && <OverviewPanel data={data} />}
      {activeTab === "movements" && <MovementsPanel movements={data.movements} selectedId={selectedMovementId} onSelect={selectMovement} />}
      {activeTab === "news" && <NewsPanel movement={selectedMovement} />}
    </> : <>
      {error ? <ErrorState error={error} onRetry={() => analyze(symbol)} /> : <DemoDataBanner onUseDemo={useDemo} />}
      <div className="px-5 py-6"><p className="text-sm text-mute">Search a ticker or use demo data.</p></div>
    </>}
    {sourceMessage(data, mode === "demo", connection) && <p className="mt-auto border-t border-line px-5 py-3 text-[11px] text-mute">{sourceMessage(data, mode === "demo", connection)}</p>}
  </div>;

  const home = <>
    <aside className="scrollbar-thin w-full shrink-0 border-b border-line bg-panel lg:w-[360px] lg:overflow-y-auto lg:border-b-0 lg:border-r">
      {research}
    </aside>
    <section className="flex min-h-[590px] min-w-0 flex-1 flex-col bg-surface p-3 sm:p-5 lg:min-h-0 lg:p-6" aria-label="Price research chart">
      <PriceChart data={data} selectedMovement={selectedMovement} onSelectMovement={selectMovement} />
    </section>
  </>;

  const ask = <section className="flex min-h-[590px] min-w-0 flex-1 flex-col bg-surface p-3 sm:p-5 lg:min-h-0 lg:p-6" aria-label="Ask Metrix workspace">
    <ChatPanel messages={messages} isLoading={chatLoading} ticker={data?.ticker.symbol} onSend={sendQuestion} onNewConversation={() => { setMessages([]); setConversationId(undefined); }} onSelectMovement={(movementId) => { const movement = data?.movements.find((item) => item.id === movementId); if (movement) selectMovement(movement); }} />
  </section>;

  return <AppShell navigation={<AppNavigation active={page} onChange={navigate} />}>{page === "home" ? home : ask}</AppShell>;
}
