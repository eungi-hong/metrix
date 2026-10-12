import { BarChart3, Home, MessageSquareMore } from "lucide-react";

export type WorkspacePage = "home" | "ask";

const navigationItems: { label: string; page: WorkspacePage; icon: typeof Home }[] = [
  { label: "Home", page: "home", icon: Home },
  { label: "Ask Metrix", page: "ask", icon: MessageSquareMore },
];

export function AppNavigation({ active, onChange }: { active: WorkspacePage; onChange: (page: WorkspacePage) => void }) {
  return (
    <nav aria-label="Metrix workspace" className="flex h-full min-h-full flex-col bg-surface px-3 py-4">
      <button type="button" onClick={() => onChange("home")} className="flex items-center gap-2 rounded-lg px-2 py-2 text-sm font-semibold tracking-tight text-ink">
        <span className="grid size-7 place-items-center rounded-lg bg-ink text-white"><BarChart3 size={15} /></span>
        <span>metrix</span>
      </button>

      <div className="mt-7 space-y-0.5">
        {navigationItems.map(({ label, page, icon: Icon }) => {
          const selected = active === page;
          return <button key={page} type="button" onClick={() => onChange(page)} className={`flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-xs font-medium transition-colors ${selected ? "bg-surface-raised text-ink" : "text-mute hover:bg-surface-raised/70 hover:text-ink"}`} aria-current={selected ? "page" : undefined}>
              <Icon aria-hidden="true" size={14} />
              {label}
          </button>;
        })}
      </div>

      <div className="mt-auto border-t border-line pt-3">
        <p className="px-2 text-[10px] leading-4 text-mute">Grounded market research</p>
      </div>
    </nav>
  );
}
