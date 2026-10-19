import { describe, expect, it } from "vitest";
import { fallbackNote } from "./AskPage";

describe("fallbackNote", () => {
  it("says plainly when the AI service is out of credit", () => {
    expect(fallbackNote("out_of_credit")).toMatch(/^The AI service is out of credit/);
  });
  it("names the spending limit and the busy provider", () => {
    expect(fallbackNote("spend_cap")).toMatch(/spending limit/);
    expect(fallbackNote("busy")).toMatch(/busy/);
  });
  it("keeps the generic note when no reason is given", () => {
    expect(fallbackNote(null)).toBe("Summary from stored data. Not written by the model.");
  });
});
