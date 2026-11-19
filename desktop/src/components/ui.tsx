/**
 * The small pieces every window is built from.
 *
 * Copy-adapted from `web/components/ui/states.tsx` so the desktop app and the
 * dashboard are recognisably one product. The rules come with them:
 *
 * - **The label is muted; the figure never is.** Grey is for labels, not for a
 *   number someone is about to make a decision on.
 * - **A missing value is stated, not rendered as blank or zero.** A blank panel
 *   where a battery voltage belongs reads as "fine" to an operator about to fly.
 * - **Status ships icon + words.** Colour never carries the meaning alone.
 *
 * DELIBERATE DIVERGENCES from the web file (2026-09-22), NOT propagated back:
 * every corner is square (see --radius-* in styles.css), the padding is tighter
 * because a desktop window is denser than a web page, and PageHeader is set in
 * Inter rather than Fraunces. The rules above, and the colour tokens, are
 * shared and should stay shared.
 */

import type { ReactNode } from "react";

export type Tone = "good" | "warning" | "serious" | "critical" | "idle";

/** Shape AND colour, so a status never rests on colour alone (WCAG 1.4.1). */
export const TONE_ICON: Record<Tone, string> = {
  good: "●", warning: "▲", serious: "▲", critical: "■", idle: "○",
};

export const TONE_COLOR: Record<Tone, string> = {
  good: "var(--status-good)",
  warning: "var(--status-warning)",
  serious: "var(--status-serious)",
  critical: "var(--status-critical)",
  idle: "var(--muted)",
};

export function StatusDot({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden="true" style={{ color: TONE_COLOR[tone] }}>{TONE_ICON[tone]}</span>
      <span>{children}</span>
    </span>
  );
}

export function Stat({
  label, value, unit, tone, hint, digits = 2, fit = false, className = "",
}: {
  label: string;
  value: number | string | null | undefined;
  unit?: string;
  tone?: Tone;
  hint?: string;
  digits?: number;
  /** A long value (an email): shown whole at a smaller size, never cut short.
   *  Give the card the room — see the wider cell on Home. */
  fit?: boolean;
  /** Layout from the caller — a wider cell for a value that needs the room. */
  className?: string;
}) {
  const missing = value === null || value === undefined || value === "";
  const shown = typeof value === "number" ? value.toFixed(digits) : value;
  // Text values are things like an email or a hardware id: they get a smaller
  // size and break anywhere, because a 1000 px window is not the only width
  // this runs at and an overflowing figure is an unreadable one.
  const isText = typeof value === "string";

  return (
    <div className={`min-w-0 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-4 ${className}`}>
      <p className="eyebrow">{label}</p>
      <p className="mt-2 flex flex-wrap items-baseline gap-x-1.5">
        <span
          className={`min-w-0 font-bold ${
            isText ? (fit ? "wrap-anywhere text-[15px] leading-snug" : "text-base wrap-anywhere") : "tabular text-[24px] leading-none"
          } ${missing ? "text-[var(--muted)]" : "text-[var(--foreground)]"}`}
        >
          {missing ? "—" : shown}
        </span>
        {unit && !missing && <span className="text-sm text-[var(--muted)]">{unit}</span>}
      </p>
      {tone && (
        <p className="mt-2 text-xs font-medium">
          <StatusDot tone={tone}>{hint ?? ""}</StatusDot>
        </p>
      )}
      {!tone && hint && <p className="mt-1 text-xs text-[var(--muted)]">{hint}</p>}
      {missing && !hint && (
        <p className="mt-1 text-xs text-[var(--muted)]">Not available on this drone</p>
      )}
    </div>
  );
}

/**
 * A row of always-on status, as one strip rather than a row of cards.
 *
 * `Stat` is a card because on a sensor window one figure per card IS the
 * content. Home's five status values are not that: they are a masthead an
 * operator glances at, and five bordered boxes spend a third of the page
 * saying what one line can.
 *
 * The rules come with it, unchanged from `Stat`: the label is muted, the
 * FIGURE NEVER IS, and a missing value says so rather than rendering blank.
 * Tone ships as icon + words, never colour alone.
 */
export function StatusBar({ items }: {
  items: { label: string; value: string | null; tone?: Tone; hint?: string; grow?: boolean }[];
}) {
  return (
    <section
      aria-label="Status"
      className="mono flex flex-wrap items-stretch border border-[var(--border)] bg-[var(--surface)] text-xs"
    >
      {items.map((item, i) => (
        <div
          key={item.label}
          className={`min-w-0 px-3 py-2 ${i > 0 ? "border-l border-[var(--border)]" : ""} ${
            item.grow ? "flex-1" : ""
          }`}
        >
          <p className="text-[10px] uppercase tracking-[0.08em] text-[var(--muted)]">{item.label}</p>
          <p className="mt-0.5 font-semibold wrap-anywhere">
            {item.value === null || item.value === "" ? (
              <span className="text-[var(--muted)]">—</span>
            ) : item.tone ? (
              <StatusDot tone={item.tone}>{item.value}</StatusDot>
            ) : (
              item.value
            )}
          </p>
          {item.hint && (
            <p className="mt-0.5 text-[10px] leading-tight text-[var(--muted)]">{item.hint}</p>
          )}
        </div>
      ))}
    </section>
  );
}

/**
 * The title of whichever page is showing.
 *
 * Every page carries one, in the same place and the same colour, so the window
 * always answers "where am I" without the operator reading the nav back.
 *
 * Set in Inter, not Fraunces. The display serif stays on the sidebar's brand
 * mark — one place, so the app still reads as the same product as the website —
 * but a serif title sitting above a terminal and an attitude indicator reads as
 * a marketing page, not an instrument panel.
 */
export function PageHeader({ title, eyebrow, children }: {
  title: string;
  /** The section this page belongs to, when the sidebar group is not enough. */
  eyebrow?: string;
  children?: ReactNode;
}) {
  return (
    <header className="grid gap-1.5">
      {eyebrow && <p className="eyebrow">{eyebrow}</p>}
      <h1 className="wrap-anywhere text-[22px] font-bold leading-tight tracking-[-0.015em] text-[var(--heading)]">
        {title}
      </h1>
      {children && <p className="max-w-2xl text-sm leading-relaxed text-[var(--muted)]">{children}</p>}
    </header>
  );
}

export function Panel({
  title, children, action, note, className = "", bodyClassName = "",
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
  note?: ReactNode;
  /** Layout from the caller — a panel that must fill a column, for instance. */
  className?: string;
  /** For a body that owns its own padding, like the console panes. */
  bodyClassName?: string;
}) {
  return (
    <section className={`rounded-xl border border-[var(--border)] bg-[var(--surface)] ${className}`}>
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-3">
        {/* The rule is decorative (it carries no meaning colour has to convey);
            the title's own colour is the measured --heading token. Square now,
            like everything else — see --radius-* in styles.css. */}
        <h2 className="flex min-w-0 items-center gap-2.5 text-sm font-bold uppercase tracking-[0.06em] text-[var(--heading)]">
          <span aria-hidden="true" className="h-3.5 w-0.5 shrink-0 bg-[var(--primary)]" />
          {title}
        </h2>
        {action}
      </div>
      {/* The note sits outside the body so a panel whose body owns its own
          padding (the console panes) still gets a readable note. */}
      {note && (
        <p className="max-w-3xl px-5 pt-4 text-sm leading-relaxed text-[var(--muted)]">{note}</p>
      )}
      <div className={bodyClassName || "p-5"}>{children}</div>
    </section>
  );
}

export function Button({
  children, onClick, variant = "secondary", disabled, type = "button", title,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "danger";
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
}) {
  const styles = {
    primary: "bg-[var(--primary)] text-[var(--on-primary)] border-transparent",
    secondary: "border-[var(--border)] text-[var(--foreground)]",
    danger: "border-[var(--status-critical)] bg-[var(--status-critical)] text-[var(--on-critical)] font-bold",
  }[variant];

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`inline-flex min-h-11 cursor-pointer items-center justify-center gap-2 rounded-lg border px-5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${styles}`}
    >
      {children}
    </button>
  );
}

/**
 * An on/off switch with its state in words beside it ("…: on"), so the state
 * never rests on the knob's colour alone. The DPP switch at the top of Control
 * is this (the camera's auto-capture switch was too, until 2026-11-15).
 */
export function Switch({ on, label, onClick, disabled, title }: {
  on: boolean;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      disabled={disabled}
      title={title}
      onClick={onClick}
      className="inline-flex min-h-9 items-center gap-2 border border-[var(--border)] bg-[var(--surface)] px-2.5 text-xs font-semibold disabled:opacity-40"
    >
      <span aria-hidden="true"
            className={`relative inline-block h-4 w-7 rounded-full border ${on ? "border-[var(--primary)] bg-[var(--primary)]" : "border-[var(--muted)] bg-[var(--surface-2)]"}`}>
        <span className={`absolute top-0.5 h-2.5 w-2.5 rounded-full ${on ? "left-3.5 bg-[var(--on-primary)]" : "left-0.5 bg-[var(--muted)]"}`} />
      </span>
      {label}: {on ? "on" : "off"}
    </button>
  );
}

/** An error the operator can act on. Never "something went wrong" alone. */
export function Message({ text, tone }: { text: string; tone: Tone }) {
  return (
    <p
      role={tone === "critical" ? "alert" : "status"}
      className="rounded-lg border bg-[var(--surface)] px-4 py-3 text-sm"
      style={{ borderColor: TONE_COLOR[tone] }}
    >
      <StatusDot tone={tone}>{text}</StatusDot>
    </p>
  );
}

/**
 * A placeholder in the shape of the thing that is loading.
 *
 * The window opens before the agent has answered and before the saved sign-in
 * has been tried. Showing the signed-out page in that gap told the operator
 * something false and then changed under them; this holds the shape instead.
 */
export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <span aria-hidden="true" className={`block animate-pulse rounded-md bg-[var(--surface-2)] ${className}`} />
  );
}

export function SkeletonPanel({ lines = 3 }: { lines?: number }) {
  return (
    <section className="rounded-xl border border-[var(--border)] bg-[var(--surface)] p-6">
      <Skeleton className="h-5 w-40" />
      <div className="mt-5 grid gap-3">
        {Array.from({ length: lines }).map((_, i) => (
          <Skeleton key={i} className="h-4" />
        ))}
      </div>
    </section>
  );
}

export function Spinner({ label }: { label: string }) {
  return (
    <p role="status" aria-live="polite" className="flex items-center gap-3 text-sm">
      <span
        aria-hidden="true"
        className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-[var(--border)] border-t-[var(--primary)]"
      />
      {label}
    </p>
  );
}
