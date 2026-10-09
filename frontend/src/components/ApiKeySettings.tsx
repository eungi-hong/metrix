import { KeyRound, Settings2, X } from "lucide-react";
import { useEffect, useState } from "react";

export function ApiKeySettings({ apiKey, onSave, onClear }: { apiKey: string; onSave: (key: string, persist: boolean) => void; onClear: () => void }) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(apiKey);
  const [persist, setPersist] = useState(false);
  useEffect(() => setValue(apiKey), [apiKey]);
  return <>
    <button onClick={() => setOpen(true)} className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-white px-2.5 py-2 text-xs font-semibold text-mute hover:text-ink" aria-label="API key settings"><Settings2 size={14} /> API key</button>
    {open && <div className="fixed inset-0 z-50 grid place-items-center bg-[#1d1d2c]/25 p-4" role="presentation"><section role="dialog" aria-modal="true" aria-label="API key settings" className="w-full max-w-md rounded-2xl border border-line bg-white p-5 shadow-xl">
      <div className="flex items-start justify-between"><div><div className="grid size-9 place-items-center rounded-xl bg-violet-50 text-accent"><KeyRound size={18} /></div><h2 className="mt-3 text-lg font-semibold">Connect to Metrix</h2><p className="mt-1 text-sm leading-5 text-mute">Enter a local development API key. It stays in memory unless you choose this browser session.</p></div><button onClick={() => setOpen(false)} aria-label="Close settings" className="rounded-lg p-1 text-mute hover:bg-[#f4f4f8]"><X size={18} /></button></div>
      <label className="mt-5 block text-xs font-semibold">Metrix API key<input value={value} onChange={(event) => setValue(event.target.value)} type="password" autoComplete="off" spellCheck={false} placeholder="mtx_…" className="mt-1.5 block h-10 w-full rounded-xl border border-line px-3 text-sm" /></label>
      <label className="mt-3 flex items-start gap-2 text-xs leading-5 text-mute"><input checked={persist} onChange={(event) => setPersist(event.target.checked)} type="checkbox" className="mt-0.5" />Remember for this browser session only</label>
      <div className="mt-5 flex justify-between"><button onClick={() => { onClear(); setValue(""); setOpen(false); }} className="text-xs font-semibold text-mute hover:text-red-700">Clear key</button><button onClick={() => { onSave(value.trim(), persist); setOpen(false); }} className="rounded-lg bg-accent px-3 py-2 text-xs font-semibold text-white hover:bg-[#4b45c4]">Save connection</button></div>
    </section></div>}
  </>;
}
