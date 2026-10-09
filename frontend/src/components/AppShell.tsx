import type { ReactNode } from "react";

export function AppShell({ research, chart }: { research: ReactNode; chart: ReactNode }) {
  return (
    <main className="min-h-screen bg-canvas p-3 text-ink sm:p-5 lg:p-6">
      <div className="mx-auto flex min-h-[calc(100vh-3rem)] max-w-[1720px] flex-col overflow-hidden rounded-2xl border border-line bg-white shadow-panel lg:flex-row">
        <aside className="w-full shrink-0 border-b border-line bg-[#fbfcfe] lg:w-[360px] lg:border-b-0 lg:border-r">
          {research}
        </aside>
        <section className="min-h-[590px] min-w-0 flex-1 bg-[#f6f8fb] p-3 sm:p-5 lg:p-6" aria-label="Price research chart">
          {chart}
        </section>
      </div>
    </main>
  );
}
