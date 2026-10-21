import { describe, expect, it } from "vitest";
import type { PulseMover } from "../../api/types";
import { explainedFirst } from "./HomePage";

const mover = (symbol: string, evidence: PulseMover["evidence"]) => ({ symbol, evidence }) as PulseMover;

describe("explainedFirst", () => {
  it("puts explained moves first and failed research last, keeping order within each", () => {
    const out = explainedFirst([mover("A", "failed"), mover("B", "queued"), mover("C", "explained"), mover("D", "explained")]);
    expect(out.map((m) => m.symbol)).toEqual(["C", "D", "B", "A"]);
  });
});
