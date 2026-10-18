/**
 * One place that turns API failures into plain words. Internal names (quota
 * keys, "enrichment") never reach the screen: limits are described by what
 * they limit, and reset times are shown in the viewer's own time zone.
 */
import { ApiError } from "./client";
import { localTime } from "../lib/format";

export interface Problem {
  title: string;
  body: string;
  /** Stored data is still worth showing next to this problem. */
  keepData: boolean;
  retry: boolean;
  requestId: string | null;
}

function resetAt(error: ApiError, now: number): string | null {
  const seconds = error.resetSeconds ?? error.retryAfterSeconds;
  if (seconds == null) return null;
  return localTime(new Date(now + seconds * 1000));
}

function inSeconds(error: ApiError): string {
  const seconds = Math.max(1, Math.ceil(error.retryAfterSeconds ?? 30));
  return seconds < 90 ? `${seconds} seconds` : `${Math.ceil(seconds / 60)} minutes`;
}

const LIMITS: Record<string, (e: ApiError, at: string | null) => Pick<Problem, "title" | "body">> = {
  cold_ingests_per_day: (_, at) => ({
    title: "Daily limit for new tickers reached",
    body: `You've hit today's free limit for new tickers. Stored research is still available${at ? `, and the limit resets at ${at}` : ""}.`,
  }),
  refresh_per_day: (_, at) => ({
    title: "Daily refresh limit reached",
    body: `You've used today's refreshes. The data shown is the latest stored${at ? `; refreshes are available again at ${at}` : ""}.`,
  }),
  chat_per_day: (_, at) => ({
    title: "Daily question limit reached",
    body: `You've asked today's maximum number of questions${at ? `. It resets at ${at}` : ""}. Case files and charts still work.`,
  }),
  chat_per_minute: (e) => ({ title: "Too many questions at once", body: `Try again in ${inSeconds(e)}.` }),
  requests_per_minute: (e) => ({ title: "Too many requests", body: `Slow down a little and try again in ${inSeconds(e)}.` }),
  inline_ingestions: (e) => ({
    title: "Metrix is busy",
    body: `Every research slot is in use right now. Try again in ${inSeconds(e)}.`,
  }),
};

// Server messages safe to show as they are: plain sentences, no internal names.
function readable(detail: string): boolean {
  return detail.length < 200 && !/[_{}]|Traceback/.test(detail);
}

export function describeError(error: unknown, now = Date.now()): Problem {
  if (!(error instanceof ApiError)) {
    return { title: "Something went wrong", body: "This part of the page could not load. Try again.", keepData: true, retry: true, requestId: null };
  }
  const base = { keepData: true, retry: true, requestId: error.requestId };
  if (error.code === "network") {
    return { ...base, title: "Can't reach Metrix", body: "Check your connection and try again." };
  }
  if (error.status === 429) {
    const describe = LIMITS[error.scope ?? ""] ?? ((e: ApiError) => ({ title: "Limit reached", body: `Try again in ${inSeconds(e)}.` }));
    return { ...base, retry: false, ...describe(error, resetAt(error, now)) };
  }
  if (error.code === "spend_cap_reached") {
    return {
      ...base, retry: false, title: "Today's research budget is used up",
      body: `Stored research is still available. New research resumes at ${localTime(nextUtcMidnight(now))}.`,
    };
  }
  if (error.status === 401) {
    return { ...base, retry: false, title: "Sign-in needed", body: "This Metrix server needs an API key. Add one in Settings." };
  }
  if (error.status === 404) {
    return { ...base, retry: false, title: "Not found", body: readable(error.message) ? error.message : "There is nothing stored here." };
  }
  if (error.status === 422) {
    return { ...base, retry: false, title: "That didn't look right", body: readable(error.message) ? error.message : "Check the input and try again." };
  }
  if (error.code === "upstream_unavailable" || error.status === 502 || error.status === 503) {
    return { ...base, title: "A data provider isn't responding", body: `Stored research is still shown. Try again in ${inSeconds(error)}.` };
  }
  return { ...base, title: "Something went wrong on our side", body: "Try again. If it keeps happening, share the request id below." };
}

export function nextUtcMidnight(now: number): Date {
  const d = new Date(now);
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1));
}
