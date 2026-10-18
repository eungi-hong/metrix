import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Button, Kbd } from "../design/ui";
import { useSettings, type Theme } from "./settings";

function Sheet({ open, onOpenChange, title, children }: { open: boolean; onOpenChange: (open: boolean) => void; title: string; children: ReactNode }) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30" />
        <Dialog.Content
          aria-describedby={undefined}
          className="pop-in fixed left-1/2 top-[10vh] z-50 max-h-[80vh] w-[min(520px,calc(100vw-24px))] -translate-x-1/2 overflow-y-auto rounded-card border border-line bg-surface p-5 shadow-pop"
        >
          <div className="mb-4 flex items-center justify-between">
            <Dialog.Title className="text-heading text-ink">{title}</Dialog.Title>
            <Dialog.Close className="rounded-control p-1 text-mute hover:bg-surface-2 hover:text-ink" aria-label="Close"><X size={18} /></Dialog.Close>
          </div>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

const SHORTCUTS: [string, [string, string][]][] = [
  ["Anywhere", [["/ or Ctrl K", "Search"], ["g h", "Home"], ["g p", "Market pulse"], ["g a", "Ask Metrix"], ["g w", "Watchlist"], ["t", "Light or dark theme"], ["?", "This list"]]],
  ["Ticker page", [["j / k", "Next or previous move"], ["Enter", "Open the case file"], ["[ / ]", "Shorter or longer range"]]],
  ["Case file", [["a", "Ask about this move"], ["c", "Copy the link"], ["Esc", "Back to the ticker"]]],
];

export function ShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange} title="Keyboard shortcuts">
      <div className="grid gap-5">
        {SHORTCUTS.map(([group, rows]) => (
          <section key={group}>
            <h3 className="mb-2 text-caption font-semibold text-mute">{group}</h3>
            <dl className="grid gap-1.5">
              {rows.map(([keys, what]) => (
                <div key={keys} className="flex items-center justify-between text-body">
                  <dt className="text-ink">{what}</dt>
                  <dd className="flex gap-1">{keys.split(" ").filter((k) => k !== "or" && k !== "/").map((k) => <Kbd key={k}>{k}</Kbd>)}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </Sheet>
  );
}

export function SettingsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { theme, setTheme, keys, saveKeys } = useSettings();
  const [exa, setExa] = useState(keys?.exaApiKey ?? "");
  const [openai, setOpenai] = useState(keys?.openaiApiKey ?? "");
  const [remember, setRemember] = useState(false);
  return (
    <Sheet open={open} onOpenChange={onOpenChange} title="Settings">
      <fieldset className="mb-6">
        <legend className="mb-2 text-body font-semibold text-ink">Theme</legend>
        <div className="inline-flex rounded-control border border-line p-0.5" role="radiogroup">
          {(["system", "light", "dark"] as Theme[]).map((t) => (
            <button key={t} role="radio" aria-checked={theme === t} onClick={() => setTheme(t)}
              className={`h-8 rounded-[5px] px-3 text-body capitalize ${theme === t ? "bg-accent text-accent-contrast" : "text-mute hover:text-ink"}`}>
              {t === "system" ? "Match system" : t}
            </button>
          ))}
        </div>
      </fieldset>
      <section>
        <h3 className="text-body font-semibold text-ink">Research access</h3>
        <p className="mt-1 text-body text-mute">
          {keys
            ? "New research runs on your own Exa and OpenAI keys. They are sent only with your requests and are never stored by Metrix."
            : "You are using demo access: stored research is free to browse, and new research uses the demo's daily allowance. Add your own keys to research beyond it."}
        </p>
        <label className="mt-3 block text-caption font-medium text-ink">Exa API key
          <input type="password" autoComplete="off" spellCheck={false} value={exa} onChange={(e) => setExa(e.target.value)}
            className="mt-1 block h-9 w-full rounded-control border border-line-strong bg-surface px-2 text-body" />
        </label>
        <label className="mt-3 block text-caption font-medium text-ink">OpenAI API key
          <input type="password" autoComplete="off" spellCheck={false} value={openai} onChange={(e) => setOpenai(e.target.value)}
            className="mt-1 block h-9 w-full rounded-control border border-line-strong bg-surface px-2 text-body" />
        </label>
        <label className="mt-3 flex items-center gap-2 text-caption text-mute">
          <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> Remember until this tab closes
        </label>
        <div className="mt-4 flex justify-between gap-2">
          <Button variant="quiet" onClick={() => { saveKeys(null, false); setExa(""); setOpenai(""); }} disabled={!keys}>Use demo access</Button>
          <Button variant="primary" disabled={!exa.trim() || !openai.trim()}
            onClick={() => { saveKeys({ exaApiKey: exa.trim(), openaiApiKey: openai.trim() }, remember); onOpenChange(false); }}>
            Use my keys
          </Button>
        </div>
      </section>
    </Sheet>
  );
}
