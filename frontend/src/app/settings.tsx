/** Viewer preferences: theme (stored per browser) and bring-your-own provider keys (this tab only, if at all). */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { setProviderKeys, type ProviderKeys } from "../api/client";

export type Theme = "system" | "light" | "dark";
const THEME_KEY = "metrix-theme";
const KEYS_KEY = "metrix-provider-keys";

interface Settings {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  cycleTheme: () => void;
  keys: ProviderKeys | null;
  saveKeys: (keys: ProviderKeys | null, remember: boolean) => void;
}

const Ctx = createContext<Settings | null>(null);

function read<T>(storage: () => Storage, key: string, fallback: T): T {
  try {
    const raw = storage().getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function validKeys(value: unknown): ProviderKeys | null {
  const v = value as Partial<ProviderKeys> | null;
  return v && typeof v.exaApiKey === "string" && typeof v.openaiApiKey === "string" && v.exaApiKey && v.openaiApiKey
    ? { exaApiKey: v.exaApiKey, openaiApiKey: v.openaiApiKey }
    : null;
}

export function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => read(() => localStorage, THEME_KEY, "system" as Theme));
  const [keys, setKeys] = useState<ProviderKeys | null>(() => validKeys(read(() => sessionStorage, KEYS_KEY, null)));

  useEffect(() => applyTheme(theme), [theme]);
  useEffect(() => setProviderKeys(keys), [keys]);

  const setTheme = useCallback((next: Theme) => {
    setThemeState(next);
    try { localStorage.setItem(THEME_KEY, JSON.stringify(next)); } catch { /* storage may be blocked */ }
  }, []);
  const cycleTheme = useCallback(() => {
    const dark = theme === "dark" || (theme === "system" && window.matchMedia?.("(prefers-color-scheme: dark)").matches);
    setTheme(dark ? "light" : "dark");
  }, [setTheme, theme]);
  const saveKeys = useCallback((next: ProviderKeys | null, remember: boolean) => {
    setKeys(next);
    setProviderKeys(next);
    try {
      if (next && remember) sessionStorage.setItem(KEYS_KEY, JSON.stringify(next));
      else sessionStorage.removeItem(KEYS_KEY);
    } catch { /* storage may be blocked; the keys still work in memory */ }
  }, []);

  const value = useMemo(() => ({ theme, setTheme, cycleTheme, keys, saveKeys }), [theme, setTheme, cycleTheme, keys, saveKeys]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSettings(): Settings {
  const value = useContext(Ctx);
  if (!value) throw new Error("useSettings outside SettingsProvider");
  return value;
}
