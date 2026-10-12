import { KeyRound, Settings2, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { ProviderKeys } from "../api/client";

export function ApiKeySettings({ providerKeys, connected, onSave, onUseDemo, onClear }: {
  providerKeys: ProviderKeys | null;
  connected: "none" | "demo" | "own";
  onSave: (keys: ProviderKeys, persist: boolean) => void;
  onUseDemo: () => void;
  onClear: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [exaApiKey, setExaApiKey] = useState(providerKeys?.exaApiKey ?? "");
  const [openaiApiKey, setOpenaiApiKey] = useState(providerKeys?.openaiApiKey ?? "");
  const [persist, setPersist] = useState(false);
  useEffect(() => {
    setExaApiKey(providerKeys?.exaApiKey ?? "");
    setOpenaiApiKey(providerKeys?.openaiApiKey ?? "");
  }, [providerKeys]);
  const canSave = Boolean(exaApiKey.trim() && openaiApiKey.trim());
  return <>
    <button onClick={() => setOpen(true)} className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-panel px-2.5 py-2 text-xs font-semibold text-mute transition-colors hover:bg-surface hover:text-ink" aria-label="Provider key settings"><Settings2 size={14} /> {connected === "own" ? "Your keys" : "Connect"}</button>
    {open && <div className="fixed inset-0 z-50 grid place-items-center bg-ink/25 p-4" role="presentation"><section role="dialog" aria-modal="true" aria-label="Provider key settings" className="w-full max-w-md rounded-2xl border border-line bg-panel p-5 shadow-xl">
      <div className="flex items-start justify-between"><div><div className="grid size-9 place-items-center rounded-xl bg-accent-soft text-accent"><KeyRound size={18} /></div><h2 className="mt-3 text-lg font-semibold">Connect to Metrix</h2><p className="mt-1 text-sm leading-5 text-mute">Use demo access, or connect your own Exa and OpenAI keys. Personal keys are sent only with your requests and are never saved by Metrix.</p></div><button onClick={() => setOpen(false)} aria-label="Close settings" className="rounded-lg p-1 text-mute hover:bg-surface-raised"><X size={18} /></button></div>
      <button onClick={() => { onUseDemo(); setOpen(false); }} className="mt-5 w-full rounded-xl border border-accent-line bg-accent-soft px-3 py-3 text-left text-xs transition-colors hover:bg-accent-line"><span className="block font-semibold text-ink">Use demo access</span><span className="mt-0.5 block leading-5 text-mute">Uses the live demo's capped server-side keys. No provider key is exposed to this browser.</span></button>
      <div className="my-5 flex items-center gap-3 text-[11px] font-semibold uppercase tracking-wide text-mute before:h-px before:flex-1 before:bg-line after:h-px after:flex-1 after:bg-line">or</div>
      <p className="text-xs font-semibold text-ink">Use my own API keys</p>
      <label className="mt-3 block text-xs font-semibold">Exa API key<input value={exaApiKey} onChange={(event) => setExaApiKey(event.target.value)} type="password" autoComplete="off" spellCheck={false} placeholder="exa_…" className="mt-1.5 block h-10 w-full rounded-xl border border-line bg-panel px-3 text-sm" /></label>
      <label className="mt-3 block text-xs font-semibold">OpenAI API key<input value={openaiApiKey} onChange={(event) => setOpenaiApiKey(event.target.value)} type="password" autoComplete="off" spellCheck={false} placeholder="sk-…" className="mt-1.5 block h-10 w-full rounded-xl border border-line bg-panel px-3 text-sm" /></label>
      <label className="mt-3 flex items-start gap-2 text-xs leading-5 text-mute"><input checked={persist} onChange={(event) => setPersist(event.target.checked)} type="checkbox" className="mt-0.5" />Remember in this browser session only</label>
      <div className="mt-5 flex justify-between"><button onClick={() => { onClear(); setExaApiKey(""); setOpenaiApiKey(""); setOpen(false); }} className="text-xs font-semibold text-mute hover:text-negative">Clear connection</button><button disabled={!canSave} onClick={() => { onSave({ exaApiKey: exaApiKey.trim(), openaiApiKey: openaiApiKey.trim() }, persist); setOpen(false); }} className="rounded-lg bg-accent px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-40">Use my keys</button></div>
    </section></div>}
  </>;
}
