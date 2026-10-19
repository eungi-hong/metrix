/** `/` or Cmd+K: find a ticker, open a case file, or ask a question. */
import * as Dialog from "@radix-ui/react-dialog";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { Command } from "cmdk";
import { FileText, MessageSquare, Search } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { keys, useDebounced } from "../api/queries";
import { longDate } from "../lib/format";
import { parseCaseIntent } from "./palette";

export function CommandPalette({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const navigate = useNavigate();
  const [input, setInput] = useState("");
  const q = useDebounced(input.trim(), 120);
  const search = useQuery({
    queryKey: keys.search(q),
    queryFn: ({ signal }) => api.searchSymbols(q, signal),
    enabled: open && q.length > 0 && !q.includes(" "),
    placeholderData: keepPreviousData, // the list never blanks while the next answer loads
    staleTime: 300_000,
    retry: false,
  });
  const intent = parseCaseIntent(input);

  const go = (path: string) => {
    onOpenChange(false);
    setInput("");
    navigate(path);
  };

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30" />
        <Dialog.Content
          aria-describedby={undefined}
          className="pop-in fixed left-1/2 top-[12vh] z-50 w-[min(640px,calc(100vw-24px))] -translate-x-1/2 overflow-hidden rounded-card border border-line bg-surface shadow-pop"
        >
          <Dialog.Title className="sr-only">Search Metrix</Dialog.Title>
          <Command label="Search tickers, case files and questions" shouldFilter={false} loop>
            <div className="flex items-center gap-2 border-b border-line px-4">
              <Search size={16} className="text-mute" aria-hidden />
              <Command.Input
                value={input}
                onValueChange={setInput}
                placeholder="NVDA, a company, NVDA aug 27, or a question"
                className="h-12 w-full bg-transparent text-body-lg text-ink outline-none placeholder:text-mute"
              />
            </div>
            <Command.List className="max-h-[60vh] overflow-y-auto p-2">
              {!input.trim() && (
                <Command.Group heading="Go to" className="text-caption text-mute [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:py-1.5">
                  {[
                    ["Home", "/"], ["Market pulse", "/pulse"], ["Ask Metrix", "/ask"], ["Watchlist", "/watchlist"], ["Methodology", "/methodology"],
                  ].map(([label, path]) => (
                    <Item key={path} onSelect={() => go(path)}>{label}</Item>
                  ))}
                </Command.Group>
              )}
              {intent && (
                <Item onSelect={() => go(`/t/${intent.symbol}/m/${intent.date}`)}>
                  <FileText size={15} aria-hidden className="text-mute" />
                  Case file: {intent.symbol}, {longDate(intent.date)}
                </Item>
              )}
              {(search.data?.results ?? []).filter(() => q.length > 0 && !q.includes(" ")).map((match) => (
                <Item key={match.symbol} value={match.symbol} onSelect={() => go(`/t/${match.symbol}`)}>
                  <span className="mono w-16 shrink-0 font-semibold text-ink">{match.symbol}</span>
                  <span className="min-w-0 flex-1 truncate text-ink">{match.name ?? ""}</span>
                  <span className="shrink-0 text-caption text-mute">{match.has_data ? "Researched" : "Not yet researched"}</span>
                </Item>
              ))}
              {input.trim() && (
                <Item value="ask" onSelect={() => go(`/ask?q=${encodeURIComponent(input.trim())}`)}>
                  <MessageSquare size={15} aria-hidden className="text-mute" />
                  Ask Metrix: “{input.trim()}”
                </Item>
              )}
            </Command.List>
          </Command>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function Item({ children, onSelect, value }: { children: React.ReactNode; onSelect: () => void; value?: string }) {
  return (
    <Command.Item
      value={value}
      onSelect={onSelect}
      className="flex h-10 cursor-pointer items-center gap-3 rounded-control px-2 text-body text-ink data-[selected=true]:bg-surface-2"
    >
      {children}
    </Command.Item>
  );
}
