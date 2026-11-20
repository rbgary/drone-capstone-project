/**
 * The account menu at the foot of the sidebar.
 *
 * The trigger shows an initial, not an email address: a full address crowded
 * the pages out at the window's minimum width, and it is not something an
 * operator needs to read every second. The menu shows it in full.
 *
 * `placement="up"` is the sidebar's: the trigger sits at the bottom of the
 * rail, so a menu opening downwards would be off-screen. It mirrors the web
 * sidebar's own ProfileMenu, which takes the same prop for the same reason.
 */

import { useEffect, useRef, useState } from "react";
import type { Session } from "@/lib/agent";
import { initialOf } from "@/lib/format";

type Props = {
  session: Session | null;
  onSignIn: () => void;
  onSignOut: () => void;
  placement?: "up" | "down";
  /** In the sidebar the trigger is a full-width row, not a bare avatar. */
  wide?: boolean;
};

export function ProfileMenu({ session, onSignIn, onSignOut, placement = "down", wide = false }: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const operator = session?.operator ?? null;
  const inSession = session != null && !["signed_out", "idle"].includes(session.state);

  // Close on a click outside or Escape — the two ways people dismiss a menu.
  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={operator ? `Account: ${operator.email}` : "Account: not signed in"}
        onClick={() => setOpen((v) => !v)}
        className={`flex min-h-10 items-center text-sm transition-colors ${
          wide
            ? "w-full gap-2.5 px-2 text-left font-medium text-[var(--foreground)] hover:bg-[var(--surface-2)]"
            : "h-10 w-10 justify-center font-semibold"
        }`}
      >
        <span
          aria-hidden="true"
          className={`flex shrink-0 items-center justify-center rounded-full font-semibold ${
            wide ? "h-8 w-8 text-xs" : "h-10 w-10 text-sm"
          } ${
            operator
              ? "bg-[var(--primary)] text-[var(--on-primary)]"
              : "border border-[var(--border)] bg-[var(--surface-2)] text-[var(--muted)]"
          }`}
        >
          {operator ? initialOf(operator.name, operator.email) : (
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth={1.8}>
              <circle cx="12" cy="8" r="4" />
              <path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6" strokeLinecap="round" />
            </svg>
          )}
        </span>
        {/* In the sidebar the row carries a name too — there is width for it
            there, and an initial alone at the foot of a rail reads as
            decoration. Never the bare email: that is what the menu is for. */}
        {wide && (
          <span className="min-w-0 flex-1 truncate">
            {operator
              ? (operator.name && operator.name !== operator.email ? operator.name : "Account")
              : "Not signed in"}
          </span>
        )}
      </button>

      {open && (
        <div
          role="menu"
          className={`absolute z-30 w-72 overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--surface)] shadow-lg ${
            placement === "up" ? "bottom-12 left-0" : "right-0 top-12"
          }`}
        >
          {operator ? (
            <>
              <div className="flex items-center gap-3 border-b border-[var(--border)] p-4">
                <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-[var(--primary)] text-base font-semibold text-[var(--on-primary)]">
                  {initialOf(operator.name, operator.email)}
                </span>
                <div className="min-w-0">
                  {operator.name && operator.name !== operator.email && (
                    <p className="truncate font-medium">{operator.name}</p>
                  )}
                  <p className="wrap-anywhere text-sm text-[var(--muted)]">{operator.email}</p>
                  {operator.role && (
                    <p className="eyebrow mt-1">{operator.role}</p>
                  )}
                </div>
              </div>
              <div className="p-2">
                {inSession && (
                  <p className="px-3 pb-2 pt-1 text-xs text-[var(--muted)]">
                    Signing out ends the running session first — landing the drone if it is flying.
                  </p>
                )}
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { setOpen(false); onSignOut(); }}
                  className="flex min-h-10 w-full items-center rounded-lg px-3 text-left text-sm font-medium text-[var(--status-critical)] hover:bg-[var(--surface-2)]"
                >
                  Sign out
                </button>
              </div>
            </>
          ) : (
            <div className="p-4">
              <p className="font-medium">Not signed in</p>
              <p className="mt-1 text-sm text-[var(--muted)]">
                Sign in with your DroneDeck account to fly. You stay signed in until you sign out.
              </p>
              <button
                type="button"
                role="menuitem"
                onClick={() => { setOpen(false); onSignIn(); }}
                className="mt-3 flex min-h-10 w-full items-center justify-center rounded-lg bg-[var(--primary)] text-sm font-medium text-[var(--on-primary)]"
              >
                Sign in
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
