/** Shared primitives. Colour only ever adds to a sign, a glyph or a word. */
import { forwardRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { componentLabel, glyph, pct, pp } from "../lib/format";
import type { Problem } from "../api/errors";

export function cx(...names: (string | false | null | undefined)[]) {
  return names.filter(Boolean).join(" ");
}

/** A signed return with its glyph: "▲ +8.7%". */
export function Change({ value, digits = 1, unit = "pct", className, plainColour = false }: {
  value: number | null | undefined; digits?: number; unit?: "pct" | "pp"; className?: string; plainColour?: boolean;
}) {
  const text = unit === "pp" ? pp(value, digits) : pct(value, digits);
  const tone = value == null || plainColour ? "" : value > 0 ? "text-up" : value < 0 ? "text-down" : "";
  return (
    <span className={cx("num whitespace-nowrap", tone, className)}>
      {value != null && value !== 0 && <span aria-hidden className="mr-1 text-[0.72em]">{glyph(value)}</span>}
      {text}
    </span>
  );
}

const DRIVER_STYLE: Record<string, string> = {
  market: "bg-market",
  sector: "bg-sector",
  company: "bg-company",
};

/** Which part of a move dominated: a swatch and a word. */
export function DriverChip({ driver, compact = false }: { driver: string | null | undefined; compact?: boolean }) {
  if (!driver) return <span className="text-caption text-mute">Not decomposed</span>;
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-caption font-medium text-ink">
      <span aria-hidden className={cx("inline-block size-2 rounded-sm", DRIVER_STYLE[driver])} />
      {compact ? componentLabel[driver] : `${componentLabel[driver]}-driven`}
    </span>
  );
}

export function Skeleton({ className, label }: { className?: string; label?: string }) {
  return <div aria-hidden={!label} aria-label={label} role={label ? "status" : undefined} className={cx("skeleton", className)} />;
}

export function Panel({ children, className, as: Tag = "section", ...rest }: { children: ReactNode; className?: string; as?: "section" | "div" | "article" | "aside"; "aria-labelledby"?: string; "aria-label"?: string }) {
  return <Tag className={cx("rounded-card border border-line bg-surface", className)} {...rest}>{children}</Tag>;
}

export function SectionTitle({ id, children, aside }: { id?: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <div className="mb-3 flex items-baseline justify-between gap-3">
      <h2 id={id} className="text-body font-semibold text-ink">{children}</h2>
      {aside && <div className="text-caption text-mute">{aside}</div>}
    </div>
  );
}

type Variant = "primary" | "quiet" | "outline";

/** Button styling, also for links that look like buttons (never a button inside a link). */
export function buttonClass(variant: Variant = "outline", className?: string) {
  return cx(
    "inline-flex h-9 items-center justify-center gap-1.5 rounded-control px-3 text-body font-medium transition-colors duration-100 disabled:cursor-not-allowed disabled:opacity-40",
    variant === "primary" && "bg-accent text-accent-contrast hover:opacity-90",
    variant === "outline" && "border border-line bg-surface text-ink hover:bg-surface-2",
    variant === "quiet" && "text-mute hover:bg-surface-2 hover:text-ink",
    className,
  );
}

export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }>(
  function Button({ variant = "outline", className, ...rest }, ref) {
    return <button ref={ref} className={buttonClass(variant, className)} {...rest} />;
  },
);

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="mono inline-flex h-5 min-w-5 items-center justify-center rounded border border-line bg-surface-2 px-1 text-[11px] text-mute">{children}</kbd>;
}

export function Pill({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cx("inline-flex items-center gap-1 rounded-full border border-line bg-surface px-2.5 py-0.5 text-caption text-ink", className)}>{children}</span>;
}

/** A problem in plain words, where the action failed. Never hides stored data. */
export function Notice({ problem, onRetry, className }: { problem: Problem; onRetry?: () => void; className?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div role="alert" className={cx("rounded-card border border-line bg-warn-soft px-4 py-3", className)}>
      <p className="text-body font-semibold text-ink">{problem.title}</p>
      <p className="mt-0.5 text-body text-ink">{problem.body}</p>
      <div className="mt-2 flex flex-wrap items-center gap-3 text-caption">
        {problem.retry && onRetry && <button className="font-semibold text-ink underline underline-offset-2" onClick={onRetry}>Try again</button>}
        {problem.requestId && (
          <button className="text-mute underline underline-offset-2" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
            Details
          </button>
        )}
      </div>
      {open && problem.requestId && (
        <p className="mt-2 flex items-center gap-2 text-caption text-mute">
          Request id <code className="mono text-ink">{problem.requestId}</code>
          <button className="underline" onClick={() => void navigator.clipboard?.writeText(problem.requestId ?? "")}>Copy</button>
        </p>
      )}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-card border border-dashed border-line px-4 py-6 text-center">
      <p className="text-body font-medium text-ink">{title}</p>
      {children && <div className="mt-1 text-body text-mute">{children}</div>}
    </div>
  );
}
