import type { EvidenceState } from "../../api/types";

export function evidenceWord(state: EvidenceState | null | undefined): string {
  switch (state) {
    case "explained": return "Explained";
    case "abstained": return "No clear driver";
    case "no_evidence": return "No evidence found";
    case "failed": return "No explanation yet";
    case null: case undefined: return "Unknown";
    default: return "Evidence pending";
  }
}

export const isPending = (state: EvidenceState | null | undefined) =>
  Boolean(state && !["explained", "abstained", "no_evidence", "failed"].includes(state));

/** What to say about a move's evidence, in a visitor's words. Older APIs
 * describe failed research in retry terms; say what it means instead. */
export function evidenceDetail(state: EvidenceState | null | undefined, detail: string): string {
  return state === "failed" ? FAILED_DETAIL : detail;
}

export const FAILED_DETAIL = "The news search for this move failed, so it has no explanation yet.";
