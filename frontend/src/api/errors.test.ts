import { describe, expect, it } from "vitest";
import { ApiError, errorFrom } from "./client";
import { describeError, nextUtcMidnight } from "./errors";

const NOW = Date.UTC(2026, 9, 19, 12, 0, 0);
const internal = /_|enrich|quota|cold|ingest/i;

describe("problems in plain words", () => {
  it("names a limit by what it limits, with the reset in the viewer's zone", () => {
    const p = describeError(new ApiError("Over the cold_ingests_per_day quota (3)", 429, "rate_limited", "cold_ingests_per_day", 3600, 3600, "r1"), NOW);
    expect(p.title).toBe("Daily limit for new tickers reached");
    expect(p.body).toMatch(/^You've hit today's free limit for new tickers\. Stored research is still available, and the limit resets at \d{1,2}:\d{2} (am|pm) /);
    expect(`${p.title} ${p.body}`).not.toMatch(internal);
    expect(p.keepData).toBe(true);
    expect(p.requestId).toBe("r1");
  });
  it("never shows internal names, whatever the server said", () => {
    for (const error of [
      new ApiError("Over the chat_per_day quota", 429, "rate_limited", "chat_per_day", 60, 7200),
      new ApiError("Over the requests_per_minute quota", 429, "rate_limited", "requests_per_minute", 20, 20),
      new ApiError("Over the something_new quota", 429, "rate_limited", "something_new", 5, 5),
      new ApiError("spend cap: background_share exceeded", 503, "spend_cap_reached"),
      new ApiError("exa: HTTP 500 in enrich_movement", 502, "upstream_unavailable", null, 30),
      new ApiError("KeyError: 'movement_id'", 500, "internal_error"),
      new ApiError("x", 422, "invalid_request"),
    ]) {
      const p = describeError(error, NOW);
      expect(`${p.title} ${p.body}`).not.toMatch(internal);
    }
  });
  it("passes a readable server sentence through for 404 and 422", () => {
    expect(describeError(new ApiError("'ZZZZ' is not a listed US symbol.", 404, "not_found")).body).toBe("'ZZZZ' is not a listed US symbol.");
  });
  it("explains a lost connection and anything unexpected", () => {
    expect(describeError(new ApiError("network", 0, "network")).title).toBe("Can't reach Metrix");
    expect(describeError(new Error("boom")).retry).toBe(true);
  });
  it("reads the headers a 429 carries", async () => {
    const response = new Response(JSON.stringify({ error: "rate_limited", detail: "Over" }), {
      status: 429, headers: { "Retry-After": "20", "X-RateLimit-Reset": "40", "X-RateLimit-Scope": "chat_per_minute", "X-Request-ID": "abc" },
    });
    const e = await errorFrom(response);
    expect([e.status, e.code, e.scope, e.retryAfterSeconds, e.resetSeconds, e.requestId]).toEqual([429, "rate_limited", "chat_per_minute", 20, 40, "abc"]);
    expect(describeError(e).body).toBe("Try again in 20 seconds.");
  });
  it("puts the spend reset at the next UTC midnight", () => {
    expect(nextUtcMidnight(NOW).toISOString()).toBe("2026-10-20T00:00:00.000Z");
  });
});
