/** Parsing for the command palette: "NVDA aug 27" or "NVDA 2026-08-27" is a case file. */

const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

export interface CaseIntent {
  symbol: string;
  date: string; // YYYY-MM-DD
}

export function parseCaseIntent(input: string, today = new Date()): CaseIntent | null {
  const text = input.trim();
  const iso = text.match(/^\$?([A-Za-z]{1,6}(?:[.-][A-Za-z])?)\s+(\d{4}-\d{2}-\d{2})$/);
  if (iso) return { symbol: iso[1].toUpperCase(), date: iso[2] };
  const named = text.match(/^\$?([A-Za-z]{1,6}(?:[.-][A-Za-z])?)\s+([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?$/);
  if (!named) return null;
  const month = MONTHS.indexOf(named[2].slice(0, 3).toLowerCase());
  if (month < 0) return null;
  const day = Number(named[3]);
  let year = named[4] ? Number(named[4]) : today.getUTCFullYear();
  if (!named[4] && Date.UTC(year, month, day) > today.getTime()) year -= 1; // "aug 27" means the last one
  const date = new Date(Date.UTC(year, month, day));
  if (date.getUTCMonth() !== month) return null;
  return { symbol: named[1].toUpperCase(), date: date.toISOString().slice(0, 10) };
}
