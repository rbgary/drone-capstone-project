// This file was modified on 11/20/2026 with the assistance of Claude Code in VS Code
// (Claude Opus 5.5, Anthropic, 2026). The Hover test's hold time no longer passes
// through here: the test moved to step ① with its own settings.

/**
 * The Control page in Auto: the mission on the left, the monitor on the right.
 *
 *   left    the Mission flow — ① Plan → ② Position → ③ Flyable space →
 *           ④ Auto-correct → ⑤ Fly. In Auto this is
 *           where the operator works, so it takes the larger share.
 *   right   the monitor: FlightDeck (the vitals, the keys and the action rail —
 *           never behind a tab, pages-and-windows.txt) above the console's
 *           Vitals. The Scene moved into ③ Fly, where the mission it now
 *           draws lives.
 *
 * The divider is the same draggable, keyboard-operable window splitter as
 * Manual's, with ITS OWN saved position (dronedeck.split.control.auto):
 * dragging in one mode must not move the other.
 *
 * BELOW 1280 px THE COLUMNS STACK — the vitals and actions first, then the
 * flow, then the console — for the same reason Manual puts its controls first.
 * (It was `lg`, 1024 px; at 1024 the monitor was 294 px wide and its tabs,
 * keys and readouts ran into each other — measured 2026-09-29.) Side by side,
 * the divider cannot squeeze the monitor below 30 % of the row.
 *
 * FULL SCREEN: the whole left section — the step bar and the step — can fill
 * the window, for planning with room to see. It is the SAME flow, restyled in
 * place (not a copy in an overlay), so the step, the chosen mission and an
 * unsaved plan survive going in and out. Escape leaves it. Land and Emergency
 * stop are in its bar whenever the drone is in the air: they must never be
 * behind anything (pages-and-windows.txt), and full screen covers the strip
 * that normally carries them. L still lands from the keyboard, as everywhere.
 *
 * Manual is untouched by this file.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";
import type { History, Run } from "@/App";
import { api, type Intent, type Session, type Telemetry } from "@/lib/agent";
import type { LogLine } from "@/lib/commandLog";
import { useMediaQuery } from "@/lib/useMediaQuery";
import { HoldToStop } from "@/components/FlightStrip";
import { SplitPane } from "@/components/SplitPane";
import { ConsolePane } from "../ConsolePane";
import { FlightDeck } from "../ControlPage";
import { Button } from "@/components/ui";
import { MissionFlow, type FlowStart } from "./MissionFlow";

/** Side by side from here up; stacked below. */
const AUTO_WIDE = "(min-width: 1280px)";
const AIRBORNE = new Set(["mission", "manual", "program"]);

export function AutoControl({
  session, telemetry, history, intent, run, logLines, onClearLog, wide,
  height, ambient, setAmbient, start,
}: {
  session: Session;
  telemetry: Telemetry | null;
  history: History;
  intent: Intent;
  run: Run;
  logLines: LogLine[];
  onClearLog: () => void;
  wide: boolean;
  height: string;
  ambient: string;
  setAmbient: (value: string) => void;
  /** Harness only: open on a given step. Never set by the app. */
  start?: FlowStart;
}) {
  const ready = session.state === "ready" && !session.retry_required;
  const sideBySide = useMediaQuery(AUTO_WIDE) && wide;
  const [full, setFull] = useState(start?.full ?? false);
  const deck = (
    <FlightDeck intent={intent} mode={session.mode} telemetry={telemetry} session={session}
                run={run} ready={ready} height={height} ambient={ambient} />
  );
  const flow = (
    <FullScreenFrame full={full} onExit={() => setFull(false)} session={session} run={run}>
      <MissionFlow session={session} run={run} telemetry={telemetry} history={history}
                   ambient={ambient} setAmbient={setAmbient} start={start}
                   full={full} onFullScreen={() => setFull(true)}
                   heightClass={full ? "h-[calc(100vh-15rem)] min-h-[20rem]" : undefined} />
    </FullScreenFrame>
  );

  if (!sideBySide) {
    return (
      <div className="grid items-start gap-5">
        {deck}
        {flow}
        <ConsolePane telemetry={telemetry} history={history} logLines={logLines}
                     onClearLog={onClearLog} />
      </div>
    );
  }

  return (
    <SplitPane
      orientation="vertical"
      storageKey="dronedeck.split.control.auto"
      defaultFraction={0.58}
      min={0.45}
      max={0.7}
      label="Mission and monitor"
      className="min-h-[calc(100vh-9.5rem)] gap-0"
      first={<div className="flex min-w-0 flex-1 flex-col pr-3">{flow}</div>}
      second={
        // A fixed window height, like Manual's console, so the console's own
        // dividers keep their range and the log scrolls inside it.
        <div className="flex h-[calc(100vh-9.5rem)] min-w-0 flex-1 flex-col gap-3 self-start pl-3">
          {deck}
          <ConsolePane telemetry={telemetry} history={history} logLines={logLines}
                       onClearLog={onClearLog} fill />
        </div>
      }
    />
  );
}

/**
 * The mission flow, inline or filling the window. One element either way —
 * only its frame changes — so nothing inside it remounts.
 */
function FullScreenFrame({ full, onExit, session, run, children }: {
  full: boolean;
  onExit: () => void;
  session: Session;
  run: Run;
  children: ReactNode;
}) {
  const exit = useRef<HTMLButtonElement>(null);
  const opener = useRef<Element | null>(null);
  // The latest onExit, read by the key handler — so the effect below runs only
  // when full screen opens or closes, not on every 10 Hz render (which would
  // pull focus back to the Exit button each time).
  const leave = useRef(onExit);
  useEffect(() => { leave.current = onExit; }, [onExit]);
  useEffect(() => {
    if (!full) return;
    opener.current = document.activeElement;
    exit.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      // Escape inside an open menu or a field closes that, not full screen.
      if ((event.target as HTMLElement | null)?.closest("[role=menu], input, select, textarea")) return;
      event.preventDefault();
      leave.current();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (opener.current instanceof HTMLElement) opener.current.focus();
    };
  }, [full]);

  // The SAME elements in both states — only their classes change — so React
  // keeps the flow mounted: its step, its chosen mission and an unsaved plan
  // all survive going in and out. `contents` makes the wrappers vanish from
  // layout when inline.
  const airborne = AIRBORNE.has(session.activity ?? "");
  return (
    <div role={full ? "dialog" : undefined} aria-modal={full || undefined}
         aria-label={full ? "Mission planning, full screen" : undefined}
         className={full ? "fixed inset-0 z-50 flex flex-col bg-[var(--background)] text-[var(--foreground)]" : "contents"}>
      {full && (
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] bg-[var(--surface)] px-4 py-2">
          <h2 className="mono text-xs font-bold uppercase tracking-[0.1em]">Mission planning · full screen</h2>
          <div className="flex flex-wrap items-center gap-2">
            {airborne && (
              <>
                <Button onClick={() => void run(api.land, "Land")} title="Land gracefully (L)">Land (L)</Button>
                <HoldToStop onStop={() => void run(api.emergencyStop, "Emergency stop")} />
              </>
            )}
            <button ref={exit} type="button" onClick={onExit}
                    className="min-h-9 border border-[var(--border)] px-3 text-xs font-semibold hover:bg-[var(--surface-2)]">
              Exit full screen (Esc)
            </button>
          </div>
        </div>
      )}
      <div className={full ? "console-scroll min-h-0 flex-1 overflow-y-auto px-4 py-4" : "contents"}>{children}</div>
    </div>
  );
}
