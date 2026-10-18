import { describe, expect, it } from "vitest";
import { activity, linkCitations, rowsFromSummary } from "./citations";

describe("citations", () => {
  it("become chips only when final and known", () => {
    const text = "Fell 6.2% [M12] after a ban [A9, F3]. Odd [note].";
    expect(linkCitations(text, true, new Set(["M12", "A9", "F3"]))).toBe("Fell 6.2% [M12](#cite-M12) after a ban [A9](#cite-A9)[F3](#cite-F3). Odd [note].");
    expect(linkCitations("x [M1] [M2]", true, new Set(["M1"]))).toBe("x [M1](#cite-M1) ");
    expect(linkCitations("x [M1]", false)).toBe("x [M1](#pending-M1)");
  });
  it("sum up the lookups in one line", () => {
    expect(activity([{ name: "get_movements", rows: 3 }, { name: "get_filings", rows: 2 }])).toBe("Checked 3 movements and 2 filings");
    expect(activity([{ name: "get_movements", rows: 1 }, { name: "get_movements", rows: 2 }, { name: "get_movement_case", rows: 1 }, { name: "search_evidence", rows: 1 }]))
      .toBe("Checked 3 movements, 1 case file and 1 article");
    expect(activity([])).toBe("");
    expect(rowsFromSummary({ name: "get_movements", arguments: {}, summary: "3 of 12 NVDA movements" })).toBe(3);
  });
});
