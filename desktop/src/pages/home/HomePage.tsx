/**
 * Home: what the operator sees when the app opens.
 *
 * Two questions, answered before anything else is offered:
 *
 *   1. **Is everything ready?** — the agent, the account, the drone, the data.
 *   2. **What do I do next?** — one highlighted step, not a menu of eight pages.
 *
 * The steps mirror the agent's own state machine (`session.py`), so this page
 * cannot get out of step with what the agent will actually allow: it reads the
 * state it is already being sent rather than tracking progress of its own.
 *
 * WRITTEN SHORT, DELIBERATELY. This page used to carry 151 words of prose in
 * five blocks — a paragraph under every step, plus "Before you fly" and
 * "Stopping it" spelled out in full. That is onboarding: read once, then in the
 * way forever, above the thing the operator actually opens the app for. The
 * steps keep their titles and lose their paragraphs, and the two reference
 * panels moved into one disclosure that starts closed.
 */

import type { Page, Run } from "@/App";
import { api, type Mode, type Session, type SyncStatus, type Telemetry } from "@/lib/agent";
import { SignIn } from "@/pages/control/ControlPage";
import { Button, PageHeader, StatusBar, StatusDot } from "@/components/ui";

type Props = {
  session: Session | null;
  telemetry: Telemetry | null;
  sync: SyncStatus | null;
  connected: boolean;
  run: Run;
  onGo: (page: Page) => void;
};

/**
 * How far the session has got, as a number the steps below can compare against.
 * `checks_failed` sits at the same stage as `starting` — the checks are still
 * the thing in front of you.
 */
const PROGRESS: Record<Session["state"], number> = {
  signed_out: 0,
  idle: 1,
  starting: 2,
  checks_failed: 2,
  awaiting_confirmation: 3,
  ready: 4,
  busy: 4,
  ending: 4,
};

/** Step titles only. The detail that used to sit under each one is in the
 *  reference disclosure at the foot, or on Control where it is acted on. */
function steps(mode: Mode): string[] {
  return [
    "Sign in",
    "Start the session",
    "Let the checks run",
    "Confirm the area is clear",
    mode === "auto" ? "Battery & motor test, then Hover demo" : "Start the motors, then hold W to rise",
  ];
}

/** "Welcome back, Ada" — or just "Welcome back": the account row carries the
 *  email, and repeating it in a title reads as a mistake. */
function greeting(session: Session | null): string {
  const operator = session?.operator;
  if (!operator) return "DroneDeck";
  const name = operator.name && operator.name !== operator.email ? operator.name : null;
  return name ? `Welcome back, ${name}` : "Welcome back";
}

function droneValue(session: Session | null): string {
  const radio = session?.radio;
  if (!radio || radio.state === "off") return "Not connected";
  if (radio.state === "connected") return radio.hardware_id ?? "Connected";
  if (radio.state === "paused") return "Disconnected";
  return "Looking…";
}

/**
 * The drone's link when no session is running — standby.
 *
 * Signed in, the agent holds the drone so vitals and the camera show before a
 * session starts. It never arms. Disconnect frees the radio for another tool
 * (a flasher, the command line); Connect takes it back. Mid-session neither is
 * offered: End session is how a session lets go.
 */
function RadioRow({ session, run }: { session: Session; run: Run }) {
  const radio = session.radio;
  if (!radio || session.state !== "idle") return null;
  const connected = radio.state === "connected";
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border border-[var(--border)] bg-[var(--surface)] px-4 py-2 text-sm">
      <StatusDot tone={connected ? "good" : radio.state === "searching" || radio.state === "restarting" ? "warning" : "idle"}>
        {connected
          ? `Drone on standby${radio.hardware_id ? ` (${radio.hardware_id})` : ""} — vitals and camera live, motors off.`
          : radio.message ?? "No drone connected."}
      </StatusDot>
      {connected ? (
        <Button onClick={() => void run(api.disconnectDrone, "Disconnect the drone")}>
          Disconnect
        </Button>
      ) : (
        <Button onClick={() => void run(api.connectDrone, "Connect the drone")}>
          {radio.state === "searching" ? "Look now" : "Connect"}
        </Button>
      )}
    </div>
  );
}

export function HomePage({ session, telemetry, sync, connected, run, onGo }: Props) {
  // After an abnormal end the next thing in front of the operator is the checks
  // again, not flying — Retry puts them back there.
  const progress = session
    ? session.state === "ready" && session.retry_required ? PROGRESS.starting : PROGRESS[session.state]
    : 0;
  const mode = session?.mode ?? "manual";
  const vbat = telemetry?.values["pm.vbat"] ?? session?.drone?.battery_v ?? null;
  const pending = sync ? sync.pending_flights + sync.pending_events : 0;

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <PageHeader eyebrow="Operate" title={greeting(session)} />
        <Button variant="primary" onClick={() => onGo("control")}>Open Control</Button>
      </div>

      {/* One strip, not five cards. The email gets the growing cell because it
          is text and must never be cut short. */}
      <StatusBar
        items={[
          {
            label: "Agent",
            value: connected ? "Running" : "Not running",
            tone: connected ? "good" : "critical",
            hint: connected ? "On this computer" : "Restart DroneDeck",
          },
          {
            label: "Signed in",
            value: session?.operator?.email ?? "No one",
            tone: session?.operator ? "good" : "warning",
            grow: true,
          },
          {
            label: "Drone",
            value: droneValue(session),
            tone: session?.radio?.state === "connected" ? "good" : "idle",
            hint: session?.radio?.state === "connected" && !session.session_id
              ? "Standby — vitals and camera, motors off" : undefined,
          },
          {
            label: "Battery",
            value: vbat == null ? null : `${vbat.toFixed(2)} V`,
            tone: vbat == null ? "idle" : vbat < 3.75 ? "warning" : "good",
            hint: vbat != null && vbat < 3.75 ? "Below the arming threshold" : undefined,
          },
          {
            label: "Mode",
            value: mode === "auto" ? "Auto" : "Manual",
          },
          {
            label: "Data",
            value: pending === 0 ? "All synced" : `${pending} to upload`,
            tone: pending === 0 ? "good" : "warning",
          },
        ]}
      />

      {session?.state === "signed_out" && <SignIn run={run} />}

      {session && session.state !== "signed_out" && <RadioRow session={session} run={run} />}

      {/* The steps, as a rail. Titles only — the current one is the answer to
          "what now", and five paragraphs buried it. */}
      <section aria-label="What to do next" className="border border-[var(--border)] bg-[var(--surface)]">
        <h2 className="flex items-center gap-2.5 border-b border-[var(--border)] px-4 py-2 text-sm font-bold uppercase tracking-[0.06em] text-[var(--heading)]">
          <span aria-hidden="true" className="h-3.5 w-0.5 shrink-0 bg-[var(--primary)]" />
          What to do next
        </h2>
        <ol className="grid gap-0.5 p-2">
          {steps(mode).map((title, index) => {
            const done = index < progress;
            const current = index === progress;
            return (
              <li
                key={title}
                aria-current={current ? "step" : undefined}
                className={`flex items-center gap-3 border-l-2 py-1.5 pl-3 text-sm ${
                  current
                    ? "border-[var(--primary)] bg-[var(--surface-2)] font-semibold"
                    : done
                      ? "border-transparent text-[var(--muted)]"
                      : "border-transparent"
                }`}
              >
                <span
                  aria-hidden="true"
                  className={`mono flex h-5 w-5 shrink-0 items-center justify-center border text-[10px] font-bold ${
                    done
                      ? "border-[var(--status-good)] text-[var(--status-good)]"
                      : current
                        ? "border-[var(--primary)] bg-[var(--primary)] text-[var(--on-primary)]"
                        : "border-[var(--border)] text-[var(--muted)]"
                  }`}
                >
                  {done ? "✓" : index + 1}
                </span>
                <span className="min-w-0">
                  {title}
                  {done && <span className="sr-only"> — done</span>}
                </span>
              </li>
            );
          })}
        </ol>
      </section>

      {/* Reference, not dashboard. Closed on arrival: an operator needs this
          on their first flight and never again, and it was taking a third of
          the page forever. */}
      <details className="group border border-[var(--border)] bg-[var(--surface)]">
        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-2 text-sm font-bold uppercase tracking-[0.06em] text-[var(--heading)]">
          Before you fly, and how to stop it
          <span aria-hidden="true" className="text-[var(--muted)] transition-transform group-open:rotate-90">›</span>
        </summary>
        <div className="grid gap-4 border-t border-[var(--border)] p-4 text-sm leading-relaxed md:grid-cols-2">
          <div>
            <h3 className="eyebrow pb-1.5">Before you fly</h3>
            <ul className="grid gap-1.5">
              <li>The Crazyradio dongle is in a USB port on this computer.</li>
              <li>The drone is charged, switched on, and on the floor.</li>
              <li>
                Base stations powered, geometry set for this room.{" "}
                <strong>Received</strong> matters, not stored —{" "}
                <button type="button" onClick={() => onGo("positioning")} className="underline underline-offset-2">
                  Positioning
                </button>
                .
              </li>
              <li>Propellers undamaged. The battery &amp; motor test on Control checks both.</li>
            </ul>
          </div>
          <div>
            <h3 className="eyebrow pb-1.5">Stopping it</h3>
            <dl className="grid gap-1.5">
              <div>
                <dt className="font-semibold">
                  Land <kbd className="mono border border-[var(--border)] px-1 text-xs">L</kbd>
                </dt>
                <dd className="text-[var(--muted)]">
                  Descends under control. Always in the strip at the top while a session runs.
                </dd>
              </div>
              <div>
                <dt className="font-semibold">Emergency stop — hold one second</dt>
                <dd className="text-[var(--muted)]">
                  Cuts the motors, so the drone drops. No key binding, deliberately.
                </dd>
              </div>
              <div>
                <dt className="font-semibold">It also lands itself</dt>
                <dd className="text-[var(--muted)]">
                  On low battery, lost positioning, drift or a tumble — and if this window
                  stops answering.
                </dd>
              </div>
            </dl>
          </div>
        </div>
      </details>

      {sync?.last_error && (
        <p className="text-xs text-[var(--muted)]">
          <StatusDot tone="warning">Last upload attempt: {sync.last_error}</StatusDot>
        </p>
      )}
      {sync?.storage_error && (
        <p className="text-xs text-[var(--muted)]">
          <StatusDot tone="warning">{sync.storage_error}</StatusDot>
        </p>
      )}
    </div>
  );
}
