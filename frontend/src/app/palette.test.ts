import { describe, expect, it } from "vitest";
import { parseCaseIntent } from "./palette";

const today = new Date(Date.UTC(2026, 9, 19));

describe("case file intents in the palette", () => {
  it("reads ISO and written dates", () => {
    expect(parseCaseIntent("NVDA 2026-08-27", today)).toEqual({ symbol: "NVDA", date: "2026-08-27" });
    expect(parseCaseIntent("nvda aug 27", today)).toEqual({ symbol: "NVDA", date: "2026-08-27" });
    expect(parseCaseIntent("$brk.b September 3, 2025", today)).toEqual({ symbol: "BRK.B", date: "2025-09-03" });
  });
  it("means the most recent past date when no year is given", () => {
    expect(parseCaseIntent("AAPL dec 5", today)).toEqual({ symbol: "AAPL", date: "2025-12-05" });
  });
  it("ignores everything else", () => {
    expect(parseCaseIntent("NVDA", today)).toBeNull();
    expect(parseCaseIntent("why did nvda fall", today)).toBeNull();
    expect(parseCaseIntent("NVDA feb 31", today)).toBeNull();
  });
});
