/** The frame: a top bar on wider screens, bottom tabs on phones, and the global shortcuts. */
import { Home, LineChart, MessageSquare, Moon, Search, Settings2, Star } from "lucide-react";
import { Suspense, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { Kbd, Skeleton, cx } from "../design/ui";
import { CommandPalette } from "./CommandPalette";
import { SettingsDialog, ShortcutsDialog } from "./Dialogs";
import { useSettings } from "./settings";

const NAV = [
  { to: "/pulse", label: "Pulse" },
  { to: "/ask", label: "Ask" },
  { to: "/watchlist", label: "Watchlist" },
  { to: "/methodology", label: "Methodology" },
];

function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  return Boolean(el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable));
}

export function Shell() {
  const navigate = useNavigate();
  const { keys, cycleTheme } = useSettings();
  const [palette, setPalette] = useState(false);
  const [shortcuts, setShortcuts] = useState(false);
  const [settings, setSettings] = useState(false);
  const pendingG = useRef(0);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPalette(true);
        return;
      }
      if (typing(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === "/") { event.preventDefault(); setPalette(true); return; }
      if (event.key === "?") { setShortcuts(true); return; }
      if (event.key === "t") { cycleTheme(); return; }
      if (event.key === "g") { pendingG.current = Date.now(); return; }
      if (Date.now() - pendingG.current < 1000) {
        const to = { h: "/", p: "/pulse", a: "/ask", w: "/watchlist", m: "/methodology" }[event.key];
        pendingG.current = 0;
        if (to) navigate(to);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cycleTheme, navigate]);

  return (
    <div className="flex min-h-screen flex-col bg-bg text-ink">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-control focus:bg-surface focus:px-3 focus:py-2">Skip to content</a>
      {import.meta.env.VITE_MSW === "1" && (
        <p className="bg-surface-2 px-4 py-1 text-center text-caption text-mute">Sample data: fictional companies, outlets and prices, served by a mocked API.</p>
      )}
      <header className="sticky top-0 z-30 border-b border-line bg-surface/95 backdrop-blur">
        <div className="mx-auto flex h-12 max-w-[1376px] items-center gap-2 px-4 md:gap-6 md:px-8">
          <NavLink to="/" className="text-[17px] font-semibold tracking-[-0.02em] text-ink" aria-label="Metrix home">metrix</NavLink>
          <nav aria-label="Main" className="hidden items-center gap-1 md:flex">
            {NAV.map((item) => (
              <NavLink key={item.to} to={item.to}
                className={({ isActive }) => cx("rounded-control px-2.5 py-1.5 text-body", isActive ? "bg-surface-2 font-medium text-ink" : "text-mute hover:text-ink")}>
                {item.label}
              </NavLink>
            ))}
          </nav>
          <button onClick={() => setPalette(true)}
            className="ml-auto flex h-8 w-full max-w-[280px] items-center gap-2 rounded-control border border-line bg-bg px-2.5 text-body text-mute hover:border-line-strong">
            <Search size={14} aria-hidden /> <span className="flex-1 text-left">Search tickers</span> <span className="hidden md:inline"><Kbd>/</Kbd></span>
          </button>
          {!keys && <span className="hidden whitespace-nowrap text-caption text-mute lg:inline" title="New research uses the demo's daily allowance">Demo access</span>}
          <button onClick={cycleTheme} className="rounded-control p-1.5 text-mute hover:bg-surface-2 hover:text-ink" aria-label="Switch light or dark theme">
            <Moon size={16} aria-hidden />
          </button>
          <button onClick={() => setSettings(true)} className="rounded-control p-1.5 text-mute hover:bg-surface-2 hover:text-ink" aria-label="Settings">
            <Settings2 size={16} aria-hidden />
          </button>
        </div>
      </header>

      <main id="main" className="mx-auto w-full max-w-[1376px] flex-1 px-4 pb-24 pt-5 md:px-8 md:pb-12 md:pt-7">
        <Suspense fallback={<Skeleton className="h-[60vh] w-full" label="Loading" />}>
          <Outlet />
        </Suspense>
      </main>

      <nav aria-label="Main" className="fixed inset-x-0 bottom-0 z-30 grid h-14 grid-cols-4 border-t border-line bg-surface pb-[env(safe-area-inset-bottom)] md:hidden">
        {[
          { to: "/", label: "Home", icon: Home, end: true },
          { to: "/pulse", label: "Pulse", icon: LineChart },
          { to: "/ask", label: "Ask", icon: MessageSquare },
          { to: "/watchlist", label: "Watchlist", icon: Star },
        ].map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end} className={({ isActive }) => cx("flex flex-col items-center justify-center gap-0.5 text-[11px]", isActive ? "font-semibold text-ink" : "text-mute")}>
            <Icon size={18} aria-hidden />{label}
          </NavLink>
        ))}
      </nav>

      <CommandPalette open={palette} onOpenChange={setPalette} />
      <ShortcutsDialog open={shortcuts} onOpenChange={setShortcuts} />
      <SettingsDialog open={settings} onOpenChange={setSettings} />
    </div>
  );
}
