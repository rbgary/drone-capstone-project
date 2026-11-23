// This file was created on 11/20/2026 with the assistance of Claude Code in VS Code
// (Claude Opus 5.5, Anthropic, 2026). It moves Auto's Hover test out of the Actions
// column into step ① Plan, above the mission list, with its height, hold and
// room-temperature settings beside the button; walks the operator through the session
// it needs; and names a drone whose motors are locked, with Reset drone, before the
// button can fly it.

/**
 * The Hover test in Auto: rise straight up, hold a steady height, land where it
 * started — a check of the drone with no mission (2026-11-20).
 *
 * It sits above the mission list in step ① Plan because it is flown INSTEAD of
 * a mission, not as part of one. Until 2026-11-20 it was the second button of
 * the Actions column (ControlPage.tsx ActionRail), on every step of the flow,
 * and its height and hold fields were in a panel Auto never rendered — so in
 * Auto it always flew the defaults.
 *
 * A hover test still needs a session: the agent runs it only once the checks
 * have passed and the area is confirmed (Session.run_program). In Auto a
 * session otherwise starts at ⑤ Fly, which needs a mission, so this panel walks
 * the operator through the same steps with the Control page's own panels
 * (Checklist, RetryPanel, HealthTestPanel) — the ones CheckStep uses — and the
 * agent's state decides what is shown, never this page.
 */

import { useEffect, useState } from "react";
import type { Run } from "@/App";
import { api, type MissionBlocker, type Session } from "@/lib/agent";
import { Button, Message, Panel, Spinner, StatusDot } from "@/components/ui";
import { Checklist, Field, HealthTestPanel, ResetDroneButton, RetryPanel } from "../ControlPage";

/** How often a ready panel asks the agent whether the motors are locked —
 *  the pace ⑤ Fly asks for a mission's blockers. */
const ASK_EVERY_MS = 2000;

export function HoverTestPanel({ session, run, ambient, setAmbient }: {
  session: Session;
  run: Run;
  ambient: string;
  setAmbient: (value: string) => void;
}) {
  const [height, setHeight] = useState("0.30");
  const [hold, setHold] = useState("10");
  const s = session.state;
  const hovering = session.activity === "program";
  const ready = s === "ready" && !session.retry_required;
  // The agent's limits (api/rest.py ProgramRequest): 0 < height <= 1 m,
  // 0 < hold <= 60 s. Said here so the button is disabled with the reason
  // rather than refused after the press.
  const h = Number(height);
  const t = Number(hold);
  const invalid = !(h > 0 && h <= 1) ? "Height must be above 0 and at most 1 m."
    : !(t > 0 && t <= 60) ? "Hold must be above 0 and at most 60 s."
    : null;

  // The drone's own motor lock, asked of the agent while the test could fly
  // (2026-11-20: a hover test right after a mission flew LOCKED, thrust 0).
  // The agent refuses on the same list, so this is the reason shown early,
  // never the gate.
  const [blockers, setBlockers] = useState<MissionBlocker[]>([]);
  useEffect(() => {
    if (!ready || !session.assisted) { setBlockers([]); return; }
    let live = true;
    const ask = () => {
      api.programBlockers()
        .then((b) => { if (live) setBlockers(b.blockers); })
        .catch(() => { if (live) setBlockers([]); });
    };
    ask();
    const timer = window.setInterval(ask, ASK_EVERY_MS);
    return () => { live = false; window.clearInterval(timer); };
  }, [ready, session.assisted, session.session_id]);
  const held = blockers.find((b) => b.code === "motors") ?? null;

  return (
    <div className="grid content-start gap-3">
      <Panel
        title="Hover test"
        note="No mission needed: rises straight up, holds a steady height, then lands where it started. The checks run again in the air — it lands by itself on low battery, lost positioning or drift."
        action={hovering ? <StatusDot tone="good">Flying</StatusDot> : undefined}
      >
        {s === "idle" ? (
          <div className="grid gap-2">
            <p className="text-sm">
              A hover test flies inside a session. Start one: it checks the drone, then you confirm the area.
            </p>
            <div>
              <Button variant="primary" onClick={() => void run(api.start, "Start session")}>Start session</Button>
            </div>
          </div>
        ) : hovering ? (
          <div className="grid gap-2">
            <Spinner label={`${session.flight?.phase ?? "flying"} ${session.flight?.detail ?? ""}`} />
            <p className="text-sm">
              Use <strong>Land</strong> at the top (or <kbd>L</kbd>) to come down early.
            </p>
          </div>
        ) : s === "busy" ? (
          <Message tone="idle" text={`The drone is busy: ${session.activity ?? "working"}. The hover test waits until it is done.`} />
        ) : s === "ending" ? (
          <Spinner label="The session is ending — landing if needed, saving the flight…" />
        ) : ready && !session.assisted ? (
          <div className="grid gap-2">
            <Message tone="warning" text={`A hover test needs the drone to know where it is. ${session.unassisted_reason ?? "The position is not usable."} Get the base stations seen, then check again.`} />
            <div><Button onClick={() => void run(api.retry, "Check the drone again")}>Check again</Button></div>
          </div>
        ) : ready ? (
          <div className="grid gap-3">
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Height (m)" value={height} onChange={setHeight}
                     type="number" step="0.05" min="0.1" max="1" />
              <Field label="Hold (s)" value={hold} onChange={setHold}
                     type="number" step="1" min="1" max="60" />
              <Field label="Room temp" value={ambient} onChange={setAmbient} hint="74F or 22C" />
            </div>
            {invalid && <Message tone="warning" text={invalid} />}
            {held && (
              <div className="grid gap-2" role="status">
                <Message tone="warning" text={`${held.message} ${held.fix}`} />
                <div><ResetDroneButton run={run} /></div>
              </div>
            )}
            <div>
              <Button
                variant="primary"
                disabled={invalid !== null || held !== null}
                onClick={() => void run(
                  () => api.runProgram(h, t, ambient),
                  `Hover test — ${height} m for ${hold} s`,
                )}
              >
                Fly hover test
              </Button>
            </div>
          </div>
        ) : (
          // starting, checks_failed, awaiting_confirmation, or a retry owed:
          // the panels below say what to do.
          <p className="text-sm">Finish the session's checks below, then the hover test can fly.</p>
        )}
      </Panel>

      {(s === "starting" || s === "checks_failed" || s === "awaiting_confirmation") && (
        <Checklist session={session} run={run} />
      )}
      {s === "ready" && session.retry_required && <RetryPanel run={run} />}
      {session.health_test && s === "ready" && <HealthTestPanel result={session.health_test} />}
    </div>
  );
}
