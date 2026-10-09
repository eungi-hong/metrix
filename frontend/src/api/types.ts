export type TickerStatus = "ready" | "ingesting" | "refreshing" | "failed";
export type Direction = "up" | "down";
export type RelevanceTier = "easy" | "medium" | "hard";
export type NewsStatus = "pending" | "complete" | "failed" | "partial";

export interface PriceBar {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  adj_close: number;
  volume: number | null;
}

export interface Article {
  id: number;
  url: string;
  title: string | null;
  source: string | null;
  author: string | null;
  published_at: string | null;
  summary: string | null;
}

export interface LinkedArticle {
  article: Article;
  relevance_tier: RelevanceTier;
  relevance_score: number;
  rationale: string | null;
  search_tier: string | null;
}

export interface Movement {
  id: number;
  date: string;
  daily_return: number;
  daily_return_pct: number;
  direction: Direction;
  prev_adj_close: number;
  adj_close: number;
  volume: number | null;
  threshold: number;
  threshold_source: string;
  rolling_std: number | null;
  sigma_multiple: number | null;
  detector_k: number;
  detector_window: number;
  detector_floor: number;
  news_status: NewsStatus;
  news: LinkedArticle[];
}

export interface Ticker {
  symbol: string;
  company_name: string | null;
  sector: string | null;
  industry: string | null;
  exchange: string | null;
  currency: string | null;
}

export interface TickerDetail {
  status: TickerStatus;
  message: string | null;
  job_id: number | null;
  ticker: Ticker;
  ingest_status: string;
  last_ingested_at: string | null;
  price_range: { start: string | null; end: string | null; bars: number };
  prices: PriceBar[] | null;
  filters: {
    start: string | null;
    end: string | null;
    min_magnitude: number | null;
    direction: Direction | null;
    tiers: RelevanceTier[] | null;
  };
  pagination: { limit: number; offset: number; total: number; returned: number };
  movements: Movement[];
  warnings: string[];
}

export interface Job {
  id: number;
  kind: string;
  status: "queued" | "running" | "succeeded" | "dead";
  source: string;
  priority: number;
  payload: Record<string, unknown>;
  progress: Record<string, unknown> | null;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  run_after: string;
  created_at: string;
  locked_at: string | null;
  finished_at: string | null;
}

export interface MovementSource {
  ref: string;
  movement_id: number;
  date: string;
  daily_return_pct: number;
  direction: Direction;
}

export interface ArticleSource {
  ref: string;
  article_id: number;
  title: string | null;
  url: string;
  source: string | null;
  published_at: string | null;
  relevance_tier: RelevanceTier;
  relevance_score: number;
}

export interface ChatSources {
  movements: MovementSource[];
  articles: ArticleSource[];
}

export interface ChatResponse {
  conversation_id: string;
  ticker: string | null;
  answer: string;
  sources: ChatSources;
  grounded: boolean;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: ChatSources;
  grounded?: boolean;
}

export interface ConversationSummary {
  id: string;
  ticker: string | null;
  created_at: string;
  last_message_at: string | null;
  messages: number;
}
