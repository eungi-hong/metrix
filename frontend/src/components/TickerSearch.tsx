import { Search, Sparkles } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";

export function TickerSearch({ initialSymbol, onAnalyze, disabled }: {
  initialSymbol: string;
  onAnalyze: (symbol: string) => void;
  disabled?: boolean;
}) {
  const [symbol, setSymbol] = useState(initialSymbol);
  useEffect(() => setSymbol(initialSymbol), [initialSymbol]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const normalized = symbol.trim().toUpperCase();
    if (normalized) onAnalyze(normalized);
  }

  return (
    <form onSubmit={submit} className="flex gap-2" aria-label="Research a ticker">
      <label className="relative min-w-0 flex-1">
        <span className="sr-only">Ticker symbol</span>
        <Search aria-hidden="true" size={16} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-mute" />
        <input
          value={symbol}
          onChange={(event) => setSymbol(event.target.value.toUpperCase())}
          maxLength={16}
          autoCapitalize="characters"
          spellCheck={false}
          placeholder="NVDA"
          className="h-10 w-full rounded-xl border border-line bg-white pl-9 pr-3 text-sm font-semibold tracking-wide placeholder:font-normal placeholder:tracking-normal placeholder:text-mute"
        />
      </label>
      <button disabled={disabled} className="inline-flex h-10 items-center gap-1.5 rounded-xl bg-accent px-3 text-sm font-semibold text-white transition-colors hover:bg-[#4b45c4] disabled:cursor-not-allowed disabled:opacity-50">
        <Sparkles aria-hidden="true" size={15} />
        Analyze
      </button>
    </form>
  );
}
