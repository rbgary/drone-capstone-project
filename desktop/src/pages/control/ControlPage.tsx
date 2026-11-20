/**
 * Control: sign in, run the checks, confirm the area, then fly.
 *
 * TWO COLUMNS, above the `lg` breakpoint:
 *
 *   left   the console — how the drone is sitting, what we asked it to do, what
 *          it is reporting, what it can see, and where it is in the room
 *   right  the controls — the action rail, then whatever this step allows
 *
 * Below `lg` they stack, controls first: at the window's 720 px minimum width
 * (tauri.conf.json) two halves would be about 230 px each, which is unusable.
 * The controls are first in the DOM for that reason and ordered back to the
 * right on wide screens.
 *
 * The order of the flow is deliberate and the agent enforces it too — this page
 * only shows what is possible at each step:
 *
 *   signed out → idle → checks → confirm the area → ready → flying
 *
 * A flight that ends abnormally (a tumble, a guard, an emergency stop) puts
 * Retry in place of the flying controls: every check again, in this session,
 * before anything can fly — the drone holds its motors after a tumble.
 *
 * Auto shows the preset programs; Manual shows arming and the keys. Both run
 * the same checks and need the same confirmation, because the risk is the same.
 *
 * AUTO HAS ITS OWN LAYOUT (auto/AutoControl.tsx, since 2026-09-28): the Mission
 * flow — ① Plan → ② Position → ③ Flyable space → ④ Auto-correct → ⑤ Fly — on the left, where the operator works,
 * and the monitor on the right. Everything below this comment is Manual's, and
 * Auto reuses its panels (FlightDeck, Checklist, RetryPanel, HealthTestPanel,
 * Field) rather than copying them.
 */

import { useState, type FormEvent, type InputHTMLAttributes, type ReactNode } from "react";
import type { History, Run } from "@/App";
import { api, KEY_LABELS, type HealthTest, type Intent, type Session, type Telemetry } from "@/lib/agent";
import type { LogLine } from "@/lib/commandLog";
import { Button, Message, PageHeader, Panel, Spinner, Stat, StatusDot, TONE_COLOR } from "@/components/ui";
import { SplitPane } from "@/components/SplitPane";
import { useMediaQuery, WIDE } from "@/lib/useMediaQuery";
import { AutoControl } from "./auto/AutoControl";
import { DataPipelinePanel, ProcessingSwitch } from "./DataPipeline";
import { ArrowFrame } from "./ArrowFrame";
import { arrowWords } from "@/lib/arrows";
import type { FlowStart } from "./auto/MissionFlow";
import { ConsolePane } from "./ConsolePane";
import { ManualMap } from "./ManualMap";
import { VitalsNow } from "./VitalsNow";

type Props = {
  /** Harness only: where the Auto flow opens (auto/MissionFlow.tsx). */
  autoStart?: FlowStart;
  session: Session | null;
  telemetry: Telemetry | null;
  history: History;
  intent: Intent;
  flying: boolean;
  run: Run;
  logLines: LogLine[];
  onClearLog: () => void;
  /** Recent sessions moved off this page; the header links to Sessions. */
  onOpenSessions: () => void;
};

export function ControlPage({
  session, telemetry, history, intent, run, logLines, onClearLog, onOpenSessions,
  autoStart,
}: Props) {
  // Below `lg` the two columns stack, so there is no divider to drag and the
  // split would be splitting nothing.
  const wide = useMediaQuery(WIDE);
  // Held here because two subtrees need them: the action rail, which starts a
  // flight, and the step panels, which set one up.
  const [height, setHeight] = useState("0.30");
  const [hold, setHold] = useState("10");
  const [ambient, setAmbient] = useState("22C");

  if (session === null) {
    return <Spinner label="Connecting to the flight agent…" />;
  }

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <PageHeader eyebrow={session.mode === "auto" ? "Auto" : "Manual"} title="Control" />
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
          {/* The DPP switch: at the top, in both modes, on by default (the
              owner, 2026-10-09) — DataPipeline.tsx. */}
          <ProcessingSwitch session={session} run={run} />
          {/* The recent-sessions list used to sit full-width at the foot of this
              page, duplicating the Sessions page and taking the width the console
              wanted. One link does the same job. */}
          <Button onClick={onOpenSessions}>Recent sessions →</Button>
        </div>
      </div>

      {session.message && (
        <Message
          tone={session.state === "checks_failed" ? "critical" : session.retry_required ? "warning" : "idle"}
          text={session.message}
        />
      )}

      {/* Signed out there is no drone and no stream, so the console would be
          a screen-and-a-half of empty terminal beside a sign-in form. The
          split starts once there is something to observe. */}
      {session.state === "signed_out" ? (
        <div className="max-w-md">
          <ControlColumn
                  session={session} telemetry={telemetry} run={run}
                  height={height} setHeight={setHeight}
                  hold={hold} setHold={setHold}
                  ambient={ambient} setAmbient={setAmbient}
                />
        </div>
      ) : session.mode === "auto" ? (
        <AutoControl
          session={session} telemetry={telemetry} history={history} intent={intent} run={run}
          logLines={logLines} onClearLog={onClearLog} wide={wide}
          height={height} hold={hold} ambient={ambient} setAmbient={setAmbient}
          start={autoStart}
        />
      ) : wide ? (
        // Side by side, with a divider the operator owns. The console starts
        // LARGER than the controls — it is the half that carries readings, and
        // the controls are mostly buttons at a fixed size.
        <SplitPane
          orientation="vertical"
          storageKey="dronedeck.split.control"
          defaultFraction={0.60}
          label="Console and controls"
          // A FLOOR, not a cap (2026-09-24): both columns grow with their
          // content and the page scrolls, instead of each column scrolling
          // inside a window-high box behind a fade.
          className="min-h-[calc(100vh-9.5rem)] gap-0"
          first={
            // A flex COLUMN, like the second: ConsolePane's `fill` is flex-1,
            // which only takes height from a flex parent. As a plain block the
            // console was content-tall — Vitals and Camera have content and
            // hid it, but a canvas-only tab (the Scene, since deleted)
            // collapsed to 0 px.
            //
            // A FIXED height, one window, and `self-start` so the row cannot
            // stretch it (2026-09-24): with the columns flowing, every new
            // line of the vitals tail made the row — and the console — taller.
            // Bounded, the tail scrolls and both dividers keep their range.
            <div className="flex h-[calc(100vh-9.5rem)] min-w-0 flex-1 flex-col self-start">
              <ConsolePane
                telemetry={telemetry}
                history={history}
                logLines={logLines}
                onClearLog={onClearLog}
                fill
                map={<ManualMap session={session} telemetry={telemetry} history={history} run={run} />}
              />
            </div>
          }
          second={
            <div className="flex min-w-0 flex-1 flex-col pl-3">
              {/* Vitals and keys are OUTSIDE the scroll. They are what an
                  operator glances at with the drone in the air, and a panel
                  that can be scrolled away is a panel that is sometimes not
                  there — the keys' own note has always said so. */}
              <FlightDeck
                intent={intent} mode={session.mode}
                telemetry={telemetry} session={session}
                run={run} ready={session.state === "ready" && !session.retry_required}
                height={height} hold={hold} ambient={ambient}
              />
              <div className="grid content-start gap-5 pt-4">
                <ControlColumn
                  session={session} telemetry={telemetry} run={run}
                  height={height} setHeight={setHeight}
                  hold={hold} setHold={setHold}
                  ambient={ambient} setAmbient={setAmbient}
                />
              </div>
            </div>
          }
        />
      ) : (
        // Stacked: there is nothing to split, and the controls come first
        // because at this width the console would push them off the screen.
        <div className="grid items-start gap-5">
          <FlightDeck
            intent={intent} mode={session.mode}
            telemetry={telemetry} session={session}
            run={run} ready={session.state === "ready" && !session.retry_required}
            height={height} hold={hold} ambient={ambient}
          />
          <ControlColumn
                  session={session} telemetry={telemetry} run={run}
                  height={height} setHeight={setHeight}
                  hold={hold} setHold={setHold}
                  ambient={ambient} setAmbient={setAmbient}
                />
          <ConsolePane
            telemetry={telemetry}
            history={history}
            logLines={logLines}
            onClearLog={onClearLog}
            map={<ManualMap session={session} telemetry={telemetry} history={history} run={run} />}
          />
        </div>
      )}
    </div>
  );
}

// ── the right column ─────────────────────────────────────────────────

function ControlColumn({ session, telemetry, run, height, setHeight, hold, setHold, ambient, setAmbient }: {
  session: Session;
  telemetry: Telemetry | null;
  run: Run;
  height: string; setHeight: (v: string) => void;
  hold: string; setHold: (v: string) => void;
  ambient: string; setAmbient: (v: string) => void;
}) {
  // The flight parameters live here, not inside a panel, because the action
  // rail's Hover demo button is their trigger and the ambient temperature is
  // shared with arming in Manual.
  const showFlightControls =
    (session.state === "ready" || session.state === "busy") && !session.retry_required;

  return (
    <>
      {session.state === "signed_out" && <SignIn run={run} />}
      {(session.state === "starting" || session.state === "checks_failed" ||
        session.state === "awaiting_confirmation") && (
        <Checklist session={session} run={run} />
      )}
      {session.state === "ready" && session.retry_required && <RetryPanel run={run} />}

      {showFlightControls && (
        <>
          {session.health_test && <HealthTestPanel result={session.health_test} />}
          {session.mode === "auto" ? (
            <AutoControls
              session={session}
              height={height} setHeight={setHeight}
              hold={hold} setHold={setHold}
              ambient={ambient} setAmbient={setAmbient}
            />
          ) : (
            <ManualControls
              session={session} telemetry={telemetry}
              ambient={ambient} setAmbient={setAmbient}
              height={height} setHeight={setHeight}
            />
          )}
        </>
      )}

      {session.state === "ending" && (
        <Panel title="Ending the session">
          <Spinner label="Landing if needed, saving the flight, and uploading what is left…" />
        </Panel>
      )}

      {/* The DPP switch (off by default in Manual) and the last flight's
          result — a Manual flight is processed as one point, "flight". */}
      {session.state !== "signed_out" && <DataPipelinePanel session={session} run={run} />}
    </>
  );
}

/**
 * The four things an operator presses, always in the same order and the same
 * place, whatever step the session is on.
 *
 * A control that cannot act right now is DISABLED WITH THE REASON, never hidden
 * and never silently dead (WCAG 2.2 AA, and Apple HIG on the same point). The
 * old page hid each of these until its step arrived, which meant the operator
 * relearned the layout at every stage.
 *
 * Slot two follows the mode: a Hover demo button in Manual would be permanently
 * disabled, which is worse than showing the thing Manual actually needs.
 */
function ActionRail({ session, run, ready, height, hold, ambient }: {
  session: Session;
  run: Run;
  ready: boolean;
  height: string;
  hold: string;
  ambient: string;
}) {
  const busy = session.state === "busy";
  const inSession = !["signed_out", "idle"].includes(session.state);

  /** Why this cannot be pressed — the agent's own constraints, in order. */
  const flightReason = (): string | null => {
    if (session.state === "signed_out") return "Sign in first — flights are recorded against a person.";
    if (session.state === "idle") return "Start the session first.";
    if (session.state === "starting") return "The checks are still running.";
    if (session.state === "checks_failed") return "The checks did not pass.";
    if (session.state === "awaiting_confirmation") return "Confirm the area is clear first.";
    if (session.state === "ending") return "The session is ending.";
    if (session.retry_required) return "Retry the checks — the last flight ended early.";
    if (busy) return "The drone is already flying. Land it first.";
    return null;
  };

  const modeSlot: RailAction = session.mode === "auto"
    ? {
        label: "Hover demo",
        variant: "primary",
        disabled: !ready || !session.assisted,
        reason: !session.assisted && flightReason() === null
          ? "The drone cannot see the base stations, so a preset flight would fly blind. Switch to Manual, or get the stations seen and start again."
          : flightReason(),
        onClick: () => void run(
          () => api.runProgram(Number(height), Number(hold), ambient),
          `Hover demo — ${height} m for ${hold} s`,
        ),
      }
    : session.activity === "manual"
      ? {
          // Once the props are turning, the useful button is the one that gets
          // the drone off the ground. This is NOT the Auto hover test: it asks
          // the manual controller for the climb W asks for, so it works on the
          // barometer — which is the state this drone is usually in.
          label: `Hover at ${height} m`,
          variant: "primary",
          disabled: false,
          onClick: () => void run(
            () => api.holdManual(Number(height)),
            `Hover at ${height} m`,
          ),
        }
      : {
          label: "Start motors",
          variant: "primary",
          disabled: !ready,
          reason: flightReason(),
          onClick: () => void run(() => api.armManual(ambient), "Start motors"),
        };

  const actions: RailAction[] = [
    {
      label: session.activity === "health_test"
        ? "Testing…"
        : session.health_test ? "Test again" : "Battery & motor test",
      disabled: !ready,
      reason: flightReason(),
      // ONE trigger, because the agent runs both tests in one call
      // (POST /session/health-test). Two buttons that fired the same request
      // would be a lie; the results below are split into two tiles instead.
      onClick: () => void run(api.healthTest, "Battery & motor test"),
    },
    modeSlot,
    {
      label: session.state === "starting" ? "Starting…" : "Start session",
      variant: "start",
      icon: POWER_ICON,
      disabled: session.state !== "idle",
      reason: session.state === "signed_out"
        ? "Sign in first — flights are recorded against a person."
        : inSession ? "A session is already running." : null,
      onClick: () => void run(api.start, "Start session"),
    },
    {
      label: "End session",
      variant: "danger",
      disabled: !inSession || busy || session.state === "ending",
      reason: !inSession
        ? "No session is running."
        : busy ? "Land the drone first (L)." : null,
      onClick: () => void run(api.endSession, "End session"),
    },
  ];

  return (
    <div className="grid content-start gap-1">
      {actions.map((action) => (
        <RailButton
          key={action.label}
          variant={action.variant ?? "secondary"}
          icon={action.icon}
          disabled={action.disabled}
          title={action.disabled ? action.reason ?? undefined : undefined}
          onClick={action.onClick}
        >
          {action.label}
        </RailButton>
      ))}
      {/* The flying action's reason is SHOWN, not only a tooltip: it is the
          line that explains why nothing happens (2026-10-01). */}
      {modeSlot.disabled && modeSlot.reason && (
        <p role="status" className="mt-1 border-l-2 pl-2 text-xs" style={{ borderColor: TONE_COLOR.warning }}>
          {modeSlot.reason}
        </p>
      )}
    </div>
  );
}

/**
 * A rail button: 32 px, not the shared Button's 44.
 *
 * WCAG 2.2 AA's target minimum is 24x24 (2.5.8); 44 is the AAA figure. These
 * are wide, in a column, with nothing adjacent to mis-hit, and the height
 * bought back is what the checks below needed. Land and Emergency stop keep
 * their 44 px in the flight strip — those are the ones reached for in a hurry.
 */
function RailButton({ children, onClick, variant = "secondary", disabled, title, icon }: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "start" | "danger";
  disabled?: boolean;
  title?: string;
  icon?: ReactNode;
}) {
  const styles = {
    primary: "bg-[var(--primary)] text-[var(--on-primary)] border-transparent",
    secondary: "border-[var(--border)] text-[var(--foreground)]",
    start: "border-[var(--status-good)] bg-[var(--status-good)] text-[var(--on-primary)] font-semibold",
    danger: "border-[var(--status-critical)] bg-[var(--status-critical)] text-[var(--on-critical)] font-bold",
  }[variant];
  return (
    <button
      type="button" onClick={onClick} disabled={disabled} title={title}
      className={`inline-flex min-h-8 w-full cursor-pointer items-center justify-center gap-1.5 border px-2 text-xs font-medium disabled:cursor-not-allowed disabled:border-[var(--border)] disabled:bg-transparent disabled:text-[var(--muted)] disabled:opacity-40 ${styles}`}
    >
      {icon}
      {children}
    </button>
  );
}

type RailAction = {
  label: string;
  onClick: () => void;
  disabled: boolean;
  reason?: string | null;
  variant?: "primary" | "secondary" | "start" | "danger";
  icon?: ReactNode;
};

/** The IEC power symbol — the one glyph everyone already reads as "on". */
const POWER_ICON = (
  <svg viewBox="0 0 24 24" aria-hidden="true" className="h-3.5 w-3.5 shrink-0"
       fill="none" stroke="currentColor" strokeWidth={2.4} strokeLinecap="round">
    <path d="M12 3v9" />
    <path d="M6.6 6.6a7.5 7.5 0 1010.8 0" />
  </svg>
);

// ── steps ────────────────────────────────────────────────────────────

export function SignIn({ run }: { run: Run }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    await run(() => api.signIn(email, password), `Sign in as ${email}`);
    setBusy(false);
  };

  return (
    <Panel
      title="Sign in"
      note="Flights are recorded against the person who flew them, so the drone will not arm until someone is signed in. Use your DroneDeck account."
    >
      <form onSubmit={submit} className="grid max-w-sm gap-3">
        <label className="grid gap-1 text-sm">
          Email
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="username"
            required
            className="min-h-10 border border-[var(--border)] bg-[var(--surface-2)] px-3"
          />
        </label>
        <label className="grid gap-1 text-sm">
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
            className="min-h-10 border border-[var(--border)] bg-[var(--surface-2)] px-3"
          />
        </label>
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
        <p className="text-xs leading-relaxed text-[var(--muted)]">
          Signing in needs an internet connection. You stay signed in on this
          computer until you sign out, and a flight can finish offline — its data
          uploads when you are back online.
        </p>
      </form>
    </Panel>
  );
}

export function Checklist({ session, run }: { session: Session; run: Run }) {
  const failed = session.state === "checks_failed";
  const awaiting = session.state === "awaiting_confirmation";
  // A positioning problem no longer blocks the flight — it costs the drone its
  // height hold. The operator is told exactly that, in the button they press.
  const unassisted = !session.assisted;

  return (
    <Panel
      title="Checks"
      note={session.state === "starting" ? "Keep the drone still while these run." : undefined}
      action={failed ? (
        session.session_id
          ? (
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" onClick={() => void run(api.retry, "Retry the checks")}>Retry</Button>
              <ResetDroneButton run={run} />
            </div>
          )
          : <Button onClick={() => void run(api.start, "Start session")}>Try again</Button>
      ) : undefined}
    >
      {awaiting && (
        // NO TICK BOXES. The button IS the acknowledgement.
        //
        // The agent requires `accept_unassisted` and refuses confirm_area
        // without it (session.py:545) — that requirement has not moved. What
        // changed is how the window obtains it. Its own docstring says the
        // operator decides "having been told it", and a button whose LABEL
        // states what is being accepted, under the warning that states why, is
        // that consent in one deliberate act instead of two. The old first box
        // ("the area is clear") gated nothing server-side at all and is now
        // simply what pressing the button means.
        //
        // The label must keep carrying the consequence. A generic "Continue"
        // here would be the UI accepting on the operator's behalf.
        //
        // ABOVE the checks, button on its first line (2026-09-24): at the foot
        // of the list it fell below the fold, and it is the one thing to press.
        // Styled as a check row — left rule, same surface — so the panel reads
        // as one list.
        <div className="mb-3 grid gap-2 border-l-2 border-[var(--status-warning)] bg-[var(--surface-2)] px-3 py-3">
          <p className="text-sm font-semibold leading-snug">
            {session.mode === "auto"
              ? "Drone on the floor and level, area clear above and around, everyone nearby warned."
              : "Drone level, area clear above and around, everyone nearby warned."}
          </p>
          {session.mode === "auto" && (
            // A mission's heights are measured from where the drone takes off
            // (CLAUDE.md invariant 4). Set down on a bench since the flight
            // now starts wherever the drone is (auto-control.txt), every point
            // — and the ceiling guard's idea of the ceiling — would ride up by
            // the bench's height, and no stored floor can catch it: the same
            // room's floor read 0.85–1.40 m across sessions.
            <p className="text-xs leading-relaxed">
              A mission's heights are measured from where the drone takes off. On a table or a bench,
              every point would fly that much higher than planned.
            </p>
          )}
          {unassisted && (
            <>
              {/* WHY, FIRST (2026-10-24): a manual flight went up on the
                  barometer, bounced by over a metre, and nothing on screen had
                  said the stations were not seen. The agent's own words. */}
              {session.unassisted_reason && (
                <p className="text-xs leading-relaxed"><strong>No base station position.</strong> {session.unassisted_reason}</p>
              )}
              <p className="text-xs leading-relaxed">
                <strong>It cannot catch itself.</strong> No base stations: height from the
                barometer, drifting tens of centimetres. No position hold, no drift
                landing. Manual only.
              </p>
            </>
          )}
          <div className="flex flex-wrap gap-2 pt-1">
            {unassisted && (
              // Looking again is the first choice: barometer flight is the
              // deliberate one (Samuel, 2026-10-24).
              <Button variant="primary" onClick={() => void run(api.retry, "Look for the base stations again")}>
                Look again for the base stations
              </Button>
            )}
            <Button
              variant={unassisted ? "secondary" : "primary"}
              onClick={() => void run(
                () => api.confirmArea(unassisted),
                unassisted ? "Confirm the area — flying by eye" : "Confirm the area",
              )}
            >
              {unassisted
                ? "Fly on the barometer — I am flying it by eye"
                : "Confirm the area is clear"}
            </Button>
          </div>
        </div>
      )}

      {/* One row per check, the glyph carrying the verdict at a fixed width so
          a check finishing never reflows the ones below it. The detail is the
          AGENT'S OWN words — it is the sentence that says what to do. */}
      <ol className="grid gap-0">
        {[...session.checks].reverse().map((check) => (
          <li
            key={check.key}
            className={`grid grid-cols-[1.25rem_5.5rem_minmax(0,1fr)] items-baseline gap-x-2 border-l-2 px-2 py-1.5 text-xs ${
              check.status === "failed" ? "border-[var(--status-critical)] bg-[var(--surface-2)]"
              : check.status === "warning" ? "border-[var(--status-warning)] bg-[var(--surface-2)]"
              : "border-transparent"
            }`}
          >
            <span aria-hidden="true" className="text-center" style={{
              color: check.status === "passed" ? "var(--status-good)"
                : check.status === "failed" ? "var(--status-critical)"
                : check.status === "warning" ? "var(--status-warning)" : "var(--muted)",
            }}>
              {check.status === "passed" ? "✓" : check.status === "failed" ? "✗"
                : check.status === "warning" ? "▲" : "·"}
            </span>
            <span className="mono font-semibold uppercase tracking-[0.06em]">{check.label}</span>
            <span className="min-w-0 wrap-anywhere leading-relaxed text-[var(--muted)]">
              {check.detail || (check.status === "running" ? "Checking…" : "")}
              <span className="sr-only"> — {check.status}</span>
            </span>
          </li>
        ))}
        {session.checks.length === 0 && (
          <li className="px-2 py-1.5"><Spinner label="Connecting to the drone…" /></li>
        )}
      </ol>

    </Panel>
  );
}

// ── after an abnormal end ────────────────────────────────────────────

export function RetryPanel({ run }: { run: Run }) {
  return (
    <Panel
      title="Check the drone again"
      note="The last flight ended early. After a tumble the drone holds its motors at zero, so a flight started now could report flying with nothing turning."
    >
      <div className="grid gap-4">
        <ol className="grid list-decimal gap-1.5 pl-5 text-sm">
          <li>Pick the drone up and look it over: propellers seated, nothing bent or loose, every deck pressed down on its pins.</li>
          <li>Stand it level on a flat surface, clear of anything it could hit.</li>
          <li>Press Retry. It frees the motors if the drone locked them, then runs every check again — the same steps as a new session, in this one.</li>
        </ol>
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" onClick={() => void run(api.retry, "Retry the checks")}>
            Retry — check the drone again
          </Button>
          <ResetDroneButton run={run} />
        </div>
        <p className="text-sm">
          After a hard crash, or if Retry does not clear it, Reset drone restarts the drone
          itself first — as if you had just switched it on and started this session.
        </p>
      </div>
    </Panel>
  );
}

/** The hard switch: motors off, the drone restarted over the radio, and every
 *  check run again — the session carries on, standing where a new one stands
 *  after its checks. A restart is the only thing that clears what a crash can
 *  leave behind in the drone (a latched motor lock, a deck not detected: decks
 *  are only detected at power-on). ~10 s: restart, reconnect, checks. */
export function ResetDroneButton({ run }: { run: Run }) {
  return (
    <Button onClick={() => void run(api.resetDrone, "Reset drone")}>
      Reset drone
    </Button>
  );
}

// ── battery & motor test ─────────────────────────────────────────────

/**
 * The result of the drone's own two ground tests.
 *
 * Its trigger is in the action rail. The agent runs both tests in a single
 * call, so there is one button and TWO result tiles — the split the operator
 * wants is in the reading, not in the request.
 */
export function HealthTestPanel({ result }: { result: HealthTest }) {
  const failed = new Set(result.motors.failed);
  return (
    <Panel
      title="Battery & motor test"
      action={
        <StatusDot tone={result.ok ? "good" : "critical"}>
          {result.ok ? "Passed" : "Did not pass"}
        </StatusDot>
      }
    >
      {/* Two columns by the PANEL's width, not the window's (2026-09-29): in
          Manual's right column at a 1024 px window, `sm:` split a 250 px panel
          in two and "M1 passed" ran into "M2 failed". */}
      <div className="@container">
      <div className="grid gap-3 @md:grid-cols-2">
        <div className="border border-[var(--border)] bg-[var(--surface-2)] p-3">
          <h3 className="eyebrow">Motors</h3>
          <ul className="mono mt-2 grid grid-cols-[repeat(auto-fit,minmax(6.5rem,1fr))] gap-1.5 text-xs">
            {[1, 2, 3, 4].map((motor) => (
              <li key={motor}>
                <StatusDot tone={failed.has(motor) ? "critical" : "good"}>
                  {`M${motor} ${failed.has(motor) ? "failed" : "passed"}`}
                </StatusDot>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs leading-relaxed text-[var(--muted)]">
            Each one spins briefly on its own, to find a bent propeller or a damaged motor.
          </p>
        </div>
        <div className="border border-[var(--border)] bg-[var(--surface-2)] p-3">
          <h3 className="eyebrow">Battery</h3>
          {result.battery ? (
            <div className="mt-2 grid gap-1 text-xs">
              <StatusDot tone={result.battery.passed ? "good" : "critical"}>
                {result.battery.passed ? "Holds up under load" : "Sags too far under load"}
              </StatusDot>
              <p className="mono text-[var(--muted)]">
                Sagged {result.battery.sag_v.toFixed(2)} V
                {result.battery.idle_vbat !== null && ` from ${result.battery.idle_vbat.toFixed(2)} V at rest`}
              </p>
            </div>
          ) : (
            <p className="mt-2 text-xs">
              <StatusDot tone="warning">{result.battery_error ?? "No result"}</StatusDot>
            </p>
          )}
          <p className="mt-2 text-xs leading-relaxed text-[var(--muted)]">
            All four run for a moment under load, to see how far the voltage sags. The
            drone does not take off — keep hands clear.
          </p>
        </div>
      </div>
      </div>
    </Panel>
  );
}

// ── auto ─────────────────────────────────────────────────────────────

function AutoControls({ session, height, setHeight, hold, setHold, ambient, setAmbient }: {
  session: Session;
  height: string;
  setHeight: (v: string) => void;
  hold: string;
  setHold: (v: string) => void;
  ambient: string;
  setAmbient: (v: string) => void;
}) {
  const running = session.activity === "program";

  return (
    <Panel
      title="Hover demo"
      note="Rises straight up, holds a steady height, then lands where it started. The checks run again in the air: it lands by itself on low battery, lost positioning or drift."
    >
      {!session.assisted ? (
        <Message
          tone="warning"
          text="A preset flight needs the drone to know where it is, and it cannot see the base stations — it would fly blind. Switch to Manual in the sidebar to fly it by hand, or get the base stations seen and start the session again."
        />
      ) : running ? (
        <div className="grid gap-2">
          <Spinner label={`${session.flight?.phase ?? "flying"} ${session.flight?.detail ?? ""}`} />
          <p className="text-sm text-[var(--muted)]">
            Use <strong>Land</strong> at the top to come down early.
          </p>
        </div>
      ) : (
        <div className="grid gap-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="Height (m)" value={height} onChange={setHeight}
                   type="number" step="0.05" min="0.1" max="1" />
            <Field label="Hold (s)" value={hold} onChange={setHold}
                   type="number" step="1" min="1" max="60" />
            <Field label="Room temp" value={ambient} onChange={setAmbient} hint="74F or 22C" />
          </div>
          <p className="text-xs text-[var(--muted)]">
            Press <strong>Hover demo</strong> in Actions above to fly it with these values.
          </p>
        </div>
      )}
    </Panel>
  );
}

export function Field({
  label, value, onChange, hint, ...input
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint?: string;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">) {
  return (
    <label className="grid gap-1 text-xs">
      <span className="eyebrow">{label}</span>
      <input
        {...input}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        // w-full: an input's intrinsic width is about 20 characters, wider
        // than the narrow cells these sit in — at the Fly step it ran under
        // the Start button beside it (harness, 2026-09-28).
        className="mono min-h-10 w-full min-w-0 border border-[var(--border)] bg-[var(--surface-2)] px-2.5 text-sm"
      />
      {hint && <span className="text-[var(--muted)]">{hint}</span>}
    </label>
  );
}

// ── manual ───────────────────────────────────────────────────────────

function ManualControls({ session, telemetry, ambient, setAmbient, height, setHeight }: {
  session: Session;
  telemetry: Telemetry | null;
  ambient: string;
  setAmbient: (v: string) => void;
  /** Shared with the action rail's "Hover at N m" button. */
  height: string;
  setHeight: (v: string) => void;
}) {
  const armed = session.activity === "manual";
  const assisted = session.assisted;

  const note = assisted
    ? armed
      ? "Hold W to rise, or press Hover to go up and hold on its own. Either way, letting go holds the height."
      : "Start motors turns the propellers gently on the ground so you can see the drone is live. It only leaves the ground when you hold W."
    : armed
      ? "Barometer height hold — roughly, wandering by tens of centimetres. Hold W to rise, or press Hover. It does not hold its position: use the arrows to correct drift."
      : "The drone cannot see the base stations, so height comes from the barometer. Start motors idles the propellers; hold W to lift off and rise gently. Land descends slowly to the floor.";

  return (
    <>
      {!assisted && session.unassisted_reason && (
        <Message tone="warning" text={`No base stations — height from the barometer, no position hold. ${session.unassisted_reason}`} />
      )}

      <Panel title={assisted ? "Manual flight" : "Manual flight — barometer height"} note={note}>
        {armed ? (
          <div className="flex flex-wrap items-end gap-5">
            <Stat label="Height" value={telemetry?.height_m ?? null} unit="m"
                  hint={assisted ? "From the base stations" : "From the barometer — approximate"} />
            <div className="w-28">
              <Field label="Hover to (m)" value={height} onChange={setHeight}
                     type="number" step="0.05" min="0.1"
                     max={assisted ? "1" : "0.8"} />
            </div>
            <p className="text-sm text-[var(--muted)]">
              <strong>Hover</strong> rises and holds; W or S takes over at any point.
              <br />
              Use <strong>Land</strong> (or press <kbd>L</kbd>) to come down.
            </p>
          </div>
        ) : (
          <div className="grid max-w-[12rem] gap-3">
            <Field label="Room temp" value={ambient} onChange={setAmbient} hint="74F or 22C" />
            <p className="text-xs text-[var(--muted)]">
              Press <strong>Start motors</strong> in Actions above.
            </p>
          </div>
        )}
      </Panel>
    </>
  );
}

/**
 * The vitals and the keys — the two things that must be on screen while the
 * drone is in the air, in the RIGHT column, above the scroll.
 *
 * IT SITS OUTSIDE THE SCROLLING AREA because scrolling counts as hidden. This
 * was a panel below the action rail, the checks and the mode controls, which
 * meant the bindings could be off screen at the moment the drone was flying;
 * then it was a full-width strip above both columns, which cost the console the
 * vertical room the command log needed. Here it is always visible and costs the
 * left column nothing.
 *
 * Laid out for a NARROW column: the vitals in three columns of two, the two key
 * clusters side by side beneath them, and Land beside those. At the widths this
 * column actually gets — roughly 400 px at a 1280 px window — a single row of
 * everything would wrap into nonsense.
 *
 * THE CAPS ARE NOT BUTTONS AND MUST NOT BECOME BUTTONS. The window reports which
 * keys are *held* and the agent runs the 50 Hz loop from that; a click has no
 * hold, so a clickable key would either do nothing or need a second control path
 * into the flight loop. They light from `intent`, which is the same state the
 * agent is being sent — so a cap lit here is a key the drone knows about.
 */
export function FlightDeck({ intent, mode, telemetry, session, run, ready, height, hold, ambient }: {
  intent: Intent;
  mode: Session["mode"];
  telemetry: Telemetry | null;
  session: Session | null;
  run: Run;
  ready: boolean;
  height: string;
  hold: string;
  ambient: string;
}) {
  const land = KEY_LABELS.find((b) => b.keys.every((k) => !k.field));
  // In Auto the drone flies itself: the movement keys reach nothing. L still
  // lands in both modes (App.tsx binds it unconditionally), so the clusters are
  // shown dimmed and said to be inactive rather than removed — a control that
  // disappears between modes is one the operator has to relearn.
  const movementLive = mode === "manual";

  return (
    <section
      aria-label="Vitals, keys and actions"
      className="shrink-0 border border-[var(--border)] bg-[var(--surface)]"
    >
      <div className="flex flex-wrap gap-4 p-3">
        {/* One column: what the drone reports, then what you press on the
            keyboard. Both are read with the same glance. */}
        <div className="grid min-w-[14rem] flex-[3] content-start gap-3">
          <VitalsNow telemetry={telemetry} session={session} />

          <div className={`flex flex-wrap items-start gap-x-5 gap-y-3 border-t border-[var(--border)] pt-3 ${movementLive ? "" : "opacity-45"}`}>
            <Cluster caption="Height · rotation">
              <div className="grid w-max grid-cols-3 gap-1">
                <span />
                <Cap label="W" field="up" intent={intent} />
                <span />
                <Cap label="A" field="yaw_left" intent={intent} />
                <Cap label="S" field="down" intent={intent} />
                <Cap label="D" field="yaw_right" intent={intent} />
              </div>
            </Cluster>

            <Cluster caption={movementLive && session ? `Position · ${arrowWords(session.controls, session.assisted).short}` : "Position"}>
              <div className="grid w-max grid-cols-3 gap-1">
                <span />
                <Cap label="↑" field="forward" intent={intent} />
                <span />
                <Cap label="←" field="left" intent={intent} />
                <Cap label="↓" field="back" intent={intent} />
                <Cap label="→" field="right" intent={intent} />
              </div>
            </Cluster>

            {land && (
              <Cluster caption="Down">
                <Cap label="L" intent={intent} wide />
              </Cluster>
            )}
          </div>

          {/* Which way the arrows go — never along the nose. On screen with
              the keys, so it can be read and switched in the air. */}
          {movementLive && session && <ArrowFrame session={session} run={run} />}
        </div>

        {/* The other column: the four things you press with the mouse. */}
        {session && (
          <div className="min-w-[9.5rem] flex-1 border-[var(--border)] sm:max-w-[11rem] sm:border-l sm:pl-4">
            <p className="eyebrow pb-1.5">Actions</p>
            <ActionRail
              session={session} run={run} ready={ready}
              height={height} hold={hold} ambient={ambient}
            />
          </div>
        )}
      </div>

      {!movementLive && (
        <p className="mono border-t border-[var(--border)] px-3 py-1.5 text-[10px] leading-relaxed text-[var(--muted)]">
          Auto flies itself — the movement keys are inactive. L still lands.
        </p>
      )}
    </section>
  );
}

function Cluster({ caption, children }: { caption: string; children: ReactNode }) {
  return (
    <div className="grid gap-1.5">
      <p className="eyebrow">{caption}</p>
      {children}
    </div>
  );
}

/** A key cap. Square, and lit while the key is held. */
function Cap({ label, field, intent, wide = false }: {
  label: string;
  field?: keyof Intent;
  intent: Intent;
  wide?: boolean;
}) {
  const held = field ? intent[field] : false;
  return (
    <kbd
      className={`mono inline-flex h-10 items-center justify-center border border-b-[3px] text-sm font-semibold ${
        wide ? "w-20" : "w-10"
      } ${
        held
          ? "border-[var(--primary)] bg-[var(--primary)] text-[var(--on-primary)]"
          : "border-[var(--border)] bg-[var(--surface-2)]"
      }`}
    >
      {label}
      {/* The lit cap is the only on-screen sign that a key registered, so the
          state has to reach a screen reader as words. Not aria-pressed: this is
          not a button, and aria-pressed on a <kbd> is invalid. */}
      {held && <span className="sr-only"> held</span>}
    </kbd>
  );
}


