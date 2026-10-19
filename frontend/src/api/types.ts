// Types generated from the API's OpenAPI document (schema.d.ts, `npm run
// gen:api`); CI fails if they are stale. Named here for readability.
import type { components } from "./schema";

type S = components["schemas"];

export type TickerDetail = S["TickerDetailOut"];
export type Ticker = S["TickerOut"];
export type Movement = S["MovementOut"];
export type PriceBar = S["PriceBarOut"];
export type LinkedArticle = S["LinkedArticleOut"];
export type Filing = S["FilingOut"];
export type Decomposition = S["DecompositionOut"];
export type EvidenceStatus = S["EvidenceStatusOut"];
export type BenchmarkSeries = S["BenchmarkSeriesOut"];
export type MarketPulse = S["MarketPulseOut"];
export type PulseMover = S["PulseMoverOut"];
export type SectorReturn = S["SectorReturnOut"];
export type MovementCase = S["MovementCaseOut"];
export type ReferenceReturn = S["ReferenceReturnOut"];
export type DetectionAudit = S["DetectionAuditOut"];
export type SymbolSearch = S["SymbolSearchOut"];
export type SymbolMatch = S["SymbolMatchOut"];
export type ChatRequest = S["ChatRequest"];
export type ChatResponse = S["ChatResponse"];
export type ChatSources = S["ChatSources"];
export type MovementSource = S["MovementSource"];
export type ArticleSource = S["ArticleSource"];
export type FilingSource = S["FilingSource"];
export type ToolCall = S["ToolCallOut"];
export type ConversationList = S["ConversationList"];
export type ConversationSummary = S["ConversationSummary"];
export type ConversationDetail = S["ConversationDetail"];
export type Job = S["JobOut"];
export type Watchlist = S["WatchlistOut"];
export type WatchlistIn = S["WatchlistIn"];
export type WatchlistDigest = S["WatchlistDigestOut"];
export type DigestHolding = S["DigestHoldingOut"];
export type EarningsOut = S["EarningsOut"];
export type TickerFilings = S["TickerFilingsOut"];
export type EvidenceState = EvidenceStatus["state"];
