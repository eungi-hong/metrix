export const shortDate = (value: string | null | undefined) => value
  ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(new Date(`${value.slice(0, 10)}T12:00:00Z`))
  : "Not available";

export const timestamp = (value: string | null | undefined) => value
  ? new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(new Date(value))
  : "Not ingested yet";

export const pct = (value: number, signed = false) => `${signed && value > 0 ? "+" : ""}${value.toFixed(2)}%`;

export const currency = (value: number | null | undefined, code = "USD") => value == null
  ? "—"
  : new Intl.NumberFormat("en-US", { style: "currency", currency: code, maximumFractionDigits: 2 }).format(value);

export const tierLabel: Record<string, string> = {
  easy: "Company",
  medium: "Industry",
  hard: "Macro",
};
