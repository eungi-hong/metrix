import type { ReactNode } from "react";

export function AppShell({ navigation, children }: { navigation: ReactNode; children: ReactNode }) {
  return (
    <main id="workspace" className="min-h-screen bg-canvas p-3 text-ink sm:p-5 lg:p-6">
      <div className="mx-auto flex min-h-[calc(100vh-3rem)] max-w-[1720px] lg:h-[calc(100vh-3rem)] lg:min-h-[620px] flex-col overflow-hidden rounded-xl border border-line bg-panel shadow-panel lg:flex-row">
        <aside className="w-full shrink-0 border-b border-line lg:w-[190px] lg:border-b-0 lg:border-r">
          {navigation}
        </aside>
        {children}
      </div>
    </main>
  );
}
