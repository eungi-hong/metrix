import { describe, expect, it } from "vitest";
import { readyDemo } from "../fixtures/demo";
import { jobFailureMessage, phaseForTicker, shouldRefetchForJob } from "./ticker";

const baseJob = { id: 1, kind: "ingest_ticker", source: "interactive", priority: 0, payload: {}, progress: null, attempts: 1, max_attempts: 3, last_error: null, run_after: "", created_at: "", locked_at: null, finished_at: null } as const;

describe("ticker and job state", () => {
  it("distinguishes queued analysis, ready data, and retained failed data", () => {
    expect(phaseForTicker(null, false)).toBe("idle");
    expect(phaseForTicker(null, true)).toBe("loading");
    expect(phaseForTicker({ ...readyDemo, status: "ingesting" }, false)).toBe("processing");
    expect(phaseForTicker(readyDemo, false)).toBe("ready");
    expect(phaseForTicker({ ...readyDemo, status: "failed" }, false)).toBe("failed");
  });

  it("only refetches after a successful job and exposes a terminal job error", () => {
    expect(shouldRefetchForJob({ ...baseJob, status: "queued" })).toBe(false);
    expect(shouldRefetchForJob({ ...baseJob, status: "succeeded" })).toBe(true);
    expect(jobFailureMessage({ ...baseJob, status: "dead", last_error: "yfinance unavailable" })).toBe("yfinance unavailable");
  });
});
