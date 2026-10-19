/**
 * Evaluation numbers, read from the committed results files at build time
 * (evals/results/*.json), never computed at request time.
 */
export interface ResultFile {
  name: string;
  file: string;
  kind: "relevance" | "chat";
  mode: string | null;
  runAt: string | null;
  command: string | null;
  metrics: Record<string, unknown>;
}

const files = import.meta.glob("../../../../evals/results/*.json", { eager: true, import: "default" }) as Record<string, Record<string, unknown>>;

export function loadResults(source: Record<string, Record<string, unknown>> = files): ResultFile[] {
  return Object.entries(source)
    .map(([path, doc]) => ({
      name: String(doc.name ?? path.split("/").pop()),
      file: `evals/results/${path.split("/").pop()}`,
      kind: (String(doc.name ?? "").startsWith("chat") || "citation_precision" in ((doc.metrics as object) ?? {}) ? "chat" : "relevance") as ResultFile["kind"],
      mode: doc.mode ? String(doc.mode) : null,
      runAt: doc.run_at ? String(doc.run_at) : null,
      command: doc.command ? String(doc.command) : null,
      metrics: (doc.metrics as Record<string, unknown>) ?? {},
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
}
