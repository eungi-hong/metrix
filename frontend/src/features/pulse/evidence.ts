import type { EvidenceState } from "../../api/types";

export function evidenceWord(state: EvidenceState | null | undefined): string {
  switch (state) {
    case "explained": return "Explained";
    case "abstained": return "No clear driver";
    case "no_evidence": return "No evidence found";
    case "failed": return "Research failed";
    case null: case undefined: return "Unknown";
    default: return "Evidence pending";
  }
}

export const isPending = (state: EvidenceState | null | undefined) =>
  Boolean(state && !["explained", "abstained", "no_evidence", "failed"].includes(state));
