/**
 * Number and date formatting. Returns always carry a sign; display uses the
 * true minus sign (U+2212), which lines up with "+" in tabular figures and is
 * read as "minus" by screen readers. Market times are New York time with the
 * zone printed; limit resets are in the viewer's own zone.
 */

export const MINUS = "−";
const NY = "America/New_York";

function signed(text: string, value: number): string {
  if (value > 0) return `+${text}`;
  if (value < 0) return `${MINUS}${text}`;
  return text;
}

/** A fraction as a signed percentage: 0.0871 -> "+8.7%". */
export function pct(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return "n/a";
  const rounded = Number((value * 100).toFixed(digits));
  return signed(`${Math.abs(rounded).toFixed(digits)}%`, rounded);
}

/** A fraction in percentage points: 0.079 -> "+7.9 pp". */
export function pp(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return "n/a";
  const rounded = Number((value * 100).toFixed(digits));
  return signed(`${Math.abs(rounded).toFixed(digits)} pp`, rounded);
}

/** A number already in percent (API fields named *_pct). */
export function pctPoints(value: number | null | undefined, digits = 1): string {
  return value == null ? "n/a" : pct(value / 100, digits);
}

export function signedNumber(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return "n/a";
  const rounded = Number(value.toFixed(digits));
  return signed(Math.abs(rounded).toFixed(digits), rounded);
}

export const glyph = (value: number | null | undefined) => (value == null || value === 0 ? "" : value > 0 ? "▲" : "▼");

export function plain(value: number | null | undefined, digits = 1): string {
  return value == null || !Number.isFinite(value) ? "n/a" : value.toFixed(digits);
}

export function times(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? "n/a" : `${value.toFixed(1)}×`;
}

export function money(value: number | null | undefined, currency = "USD"): string {
  if (value == null) return "n/a";
  return new Intl.NumberFormat("en-US", { style: "currency", currency, maximumFractionDigits: 2 }).format(value);
}

// ---- dates ------------------------------------------------------------------

function asDate(day: string): Date {
  return new Date(`${day.slice(0, 10)}T12:00:00Z`);
}

/** "2026-08-27" -> "Aug 27, 2026". */
export function longDate(day: string | null | undefined): string {
  if (!day) return "n/a";
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(asDate(day));
}

/** "2026-08-27" -> "Aug 27". */
export function shortDate(day: string | null | undefined): string {
  if (!day) return "n/a";
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" }).format(asDate(day));
}

/** An instant in New York time: "4:05 pm ET". */
export function etTime(instant: string | null | undefined): string {
  if (!instant) return "time unknown";
  const text = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone: NY }).format(new Date(instant));
  return `${text.replace("AM", "am").replace("PM", "pm")} ET`;
}

/** An instant in New York time with its date: "Aug 26, 4:05 pm ET". */
export function etDateTime(instant: string | null | undefined): string {
  if (!instant) return "time unknown";
  const day = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: NY }).format(new Date(instant));
  return `${day}, ${etTime(instant)}`;
}

/** An instant in the viewer's own zone, with the zone: "8:00 am PDT". */
export function localTime(instant: Date | string, zone?: string): string {
  const date = typeof instant === "string" ? new Date(instant) : instant;
  const text = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZoneName: "short", timeZone: zone }).format(date);
  return text.replace("AM", "am").replace("PM", "pm");
}

export function relativeAge(instant: string | null | undefined, now = Date.now()): string {
  if (!instant) return "never";
  const minutes = Math.round((now - new Date(instant).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export const tierLabel: Record<string, string> = { easy: "Company", medium: "Industry", hard: "Macro", irrelevant: "Unrelated" };
export const timingLabel: Record<string, string> = {
  before_prior_close: "Before the prior close",
  overnight: "Overnight",
  intraday: "During the session",
  after_close: "After the close",
  later: "Later",
};
export const componentLabel: Record<string, string> = { market: "Market", sector: "Sector", company: "Company" };
