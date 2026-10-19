import type { EvidenceStatus } from "../../api/types";
import { etDateTime } from "../../lib/format";

/** Why there is no evidence yet, and when there will be. */
export function EvidenceLine({ status }: { status: EvidenceStatus | null | undefined }) {
  if (!status || status.state === "explained") return null;
  const when = status.expected_by && ["queued", "window_open", "budget_deferred", "retrying", "not_started"].includes(status.state)
    ? ` Expected after ${etDateTime(status.expected_by)}.`
    : "";
  return <p className="text-body text-mute">{status.detail}{when}</p>;
}
