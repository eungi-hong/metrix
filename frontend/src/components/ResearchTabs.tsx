export type ResearchTab = "overview" | "movements" | "news" | "ask";

const tabs: { id: ResearchTab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "movements", label: "Movements" },
  { id: "news", label: "News" },
  { id: "ask", label: "Ask Metrix" },
];

export function ResearchTabs({ active, onChange }: { active: ResearchTab; onChange: (tab: ResearchTab) => void }) {
  return (
    <div role="tablist" aria-label="Research views" className="flex border-b border-line px-3">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          role="tab"
          id={`${tab.id}-tab`}
          aria-selected={active === tab.id}
          aria-controls={`${tab.id}-panel`}
          tabIndex={active === tab.id ? 0 : -1}
          onClick={() => onChange(tab.id)}
          className={`relative flex-1 whitespace-nowrap px-1 py-3 text-xs font-semibold transition-colors ${active === tab.id ? "text-ink" : "text-mute hover:text-ink"}`}
        >
          {tab.label}
          {active === tab.id && <span className="absolute inset-x-1 bottom-0 h-0.5 rounded-full bg-accent" />}
        </button>
      ))}
    </div>
  );
}
