/**
 * The left half of the Control page: what the drone is telling us.
 *
 *   Vitals   how it is sitting (the attitude indicator), what we asked it to
 *            do (the command log), and what it is reporting (the vitals tail)
 *            — in Manual, the room in place of the tail (ManualMap.tsx)
 *
 * One view, still under its strip: the strip carries the stream's status and
 * Full screen. There were two more behind tabs. Scene (the drone in a declared
 * 4 m box) went 2026-10-24 — Manual's room answers it from the base stations
 * (Samuel: "it's of no use"). Camera went 2026-11-15 with the AI deck: the
 * drone carries no camera (docs/features/architecture.txt, DECISIONS).
 *
 * TWO THINGS THE OPERATOR OWNS. A draggable divider between the attitude
 * indicator and the log — drag it up mid-flight to read more of the log, down
 * to watch the horizon — and the whole console opens full screen, because a
 * room or a horizon in part of a split column is a thumbnail.
 */

import { useState, type ReactNode } from "react";
import type { History } from "@/App";
import type { LogLine } from "@/lib/commandLog";
import type { Telemetry } from "@/lib/agent";
import { FullScreenOverlay } from "@/components/FullScreenOverlay";
import { SplitPane } from "@/components/SplitPane";
import { TabPanel, Tabs, type TabDef } from "@/components/Tabs";
import { StatusDot } from "@/components/ui";
import { AttitudeIndicator } from "./AttitudeIndicator";
import { CommandLog } from "./CommandLog";
import { VitalsTail } from "./VitalsTail";

export type ConsoleTab = "vitals";

const TABS: readonly TabDef<ConsoleTab>[] = [
  { key: "vitals", label: "Vitals" },
];

export function ConsolePane({
  telemetry, history, logLines, onClearLog, fill = false, map,
}: {
  /** Manual: the room map, between the attitude indicator and the command
   *  log, in place of the vitals tail (ManualMap.tsx — 2026-10-24, Samuel:
   *  "remove that vitals, and the bottom should just be the command log …
   *  the 3D, 2D editor should sit right below the gyro display"). The vitals
   *  are in the strip on the right (VitalsNow), so nothing is lost. */
  map?: ReactNode;
  telemetry: Telemetry | null;
  history: History;
  logLines: LogLine[];
  onClearLog: () => void;
  /** Fill the height a parent gives it (the split) instead of setting its own.
   *  Stacked, there is no parent height to fill, so it sets one. */
  fill?: boolean;
}) {
  const [tab, setTab] = useState<ConsoleTab>("vitals");
  const [full, setFull] = useState(false);

  /** `fullScreen` suppresses the controls that would be meaningless inside the
   *  overlay — a second "full screen" button, and the log's own. */
  const body = (fullScreen: boolean) => (
    <>
      <TabPanel id="console" tabKey="vitals" active={tab === "vitals"} className="flex min-h-0 flex-1 flex-col">
        <SplitPane
          orientation="horizontal"
          // Enough for the attitude indicator AND its compass strip (N/E/S/W)
          // in a one-window console at 900 px; 0.34 cut the compass letters
          // once the console stopped growing (2026-09-24). A saved position
          // still wins. With Manual's room below it, a smaller share: the
          // room is what is flown from (its own key, so Auto's is kept).
          storageKey={map ? "dronedeck.split.console.manual" : "dronedeck.split.console"}
          defaultFraction={map ? 0.34 : 0.42}
          // Folds almost away: dragging this up to read the log is the point,
          // and the shared 20 % floor made it stop a fifth of the way down with
          // nothing to explain why. Never 0 — the divider would reach the frame
          // edge and leave no handle to bring it back.
          min={0.06}
          max={0.85}
          label="Attitude indicator and the log"
          className="min-h-0 flex-1"
          first={
            <div className="console-scroll min-h-0 flex-1 overflow-y-auto">
              <AttitudeIndicator telemetry={telemetry} />
            </div>
          }
          second={map ? (
            // Manual: the room, then the log — the room first and larger, it
            // is what the operator flies from.
            <SplitPane
              orientation="horizontal"
              storageKey="dronedeck.split.maplog"
              defaultFraction={0.72}
              min={0.15}
              max={0.94}
              label="Room map and command log"
              className="min-h-0 flex-1"
              first={
                <div className="console-scroll flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto">
                  <PaneTitle>Room — from the base stations</PaneTitle>
                  {/* A flex column all the way down, so the map fills what the
                      dividers give it rather than scrolling at a fixed height. */}
                  <div className="flex min-h-0 flex-1 flex-col p-2">{map}</div>
                </div>
              }
              second={
                <div className="flex min-h-0 min-w-0 flex-1 flex-col">
                  <PaneTitle>Command log</PaneTitle>
                  <CommandLog lines={logLines} onClear={onClearLog} canExpand={!fullScreen} />
                </div>
              }
            />
          ) : (
            // The log and the vitals tail, with the same kind of divider as
            // the one above: drag it up and the vitals tail folds over the log,
            // down and the log takes the room (2026-09-24).
            <SplitPane
              orientation="horizontal"
              storageKey="dronedeck.split.logtail"
              defaultFraction={0.67}
              min={0.06}
              max={0.94}
              label="Command log and vitals"
              className="min-h-0 flex-1"
              // min-w-0 on both: each is a child of the split's row, and without
              // it the log's widest line set its width — 488 px in a 446 px pane
              // at the window's 720 px minimum (measured 2026-09-28).
              first={
                <div className="flex min-h-0 min-w-0 flex-1 flex-col">
                  <PaneTitle>Command log</PaneTitle>
                  <CommandLog lines={logLines} onClear={onClearLog} canExpand={!fullScreen} />
                </div>
              }
              second={
                <div className="flex min-h-0 min-w-0 flex-1 flex-col">
                  <PaneTitle>Vitals</PaneTitle>
                  <VitalsTail history={history} />
                </div>
              }
            />
          )}
        />
      </TabPanel>
    </>
  );

  const strip = (fullScreen: boolean) => (
    <Tabs
      id="console"
      label="Drone console view"
      tabs={TABS}
      active={tab}
      onSelect={setTab}
      action={
        <>
          <span className="mono text-[10px] uppercase tracking-[0.08em]">
            <StatusDot tone={telemetry ? "good" : "idle"}>
              {telemetry ? "10 Hz" : "no stream"}
            </StatusDot>
          </span>
          {!fullScreen && (
            <button
              type="button"
              onClick={() => setFull(true)}
              title="Show this view full screen"
              aria-label="Show this view full screen"
              className="mono min-h-8 min-w-8 whitespace-nowrap px-2 text-[10px] font-semibold uppercase tracking-[0.08em] text-[var(--muted)] hover:text-[var(--foreground)]"
            >
              Full screen ⤢
            </button>
          )}
        </>
      }
    />
  );

  return (
    <>
      <section
        aria-label="Drone console"
        // Filled by the split when there is one. Stacked it sets its own: 46rem
        // is a floor, not a preference — below about that the command log and
        // the vitals tail each get fewer than four lines once their own header
        // and footer bars are subtracted, which is not a log.
        className={`flex min-w-0 flex-col overflow-hidden border border-[var(--border)] bg-[var(--surface)] ${
          fill ? "min-h-0 flex-1" : "min-h-[46rem]"
        }`}
      >
        {strip(false)}
        {body(false)}
      </section>

      {full && (
        <FullScreenOverlay label="Console · Vitals" onClose={() => setFull(false)}>
          <div className="flex min-h-0 flex-1 flex-col bg-[var(--surface)]">
            {strip(true)}
            {body(true)}
          </div>
        </FullScreenOverlay>
      )}
    </>
  );
}

function PaneTitle({ children }: { children: string }) {
  return (
    <h3 className="mono shrink-0 border-b border-[var(--border)] bg-[var(--surface-2)] px-3 py-1.5 text-[10px] font-bold uppercase tracking-[0.1em] text-[var(--muted)]">
      {children}
    </h3>
  );
}
