// This file was modified on 11/20/2026 with the assistance of Claude Code in VS Code
// (Claude Opus 5.5, Anthropic, 2026). After a mission lands, the result offers End
// session as the primary action, shown exactly when the flight strip shows it.

/**
 * Step ⑤ of the Auto flow: fly the mission (the session and its checks come
 * first, in CheckStep, until the drone is ready and knows where it is).
 *
 * START MISSION IS AT THE TOP, disabled until it can act — and under it EVERY
 * reason it cannot, each with what to do, asked of the agent (GET …/blockers,
 * Session.mission_blockers — the very list Start refuses on). 2026-10-05: a
 * drone locked after a landing was "flown" three times with thrust 0, the app
 * saying started and landing; now the lock is a named reason with Reset drone.
 *
 * Below it: the room map (2-D or 3-D, RoomMap) with the mission loaded — the
 * room, its obstacles, THE AUTO-CORRECTED PLAN the agent will fly (GET
 * …/from-drone: the whole path moved to start at the drone, fitted into the
 * green; kept as it was at takeoff while flying), each point done,
 * current or ahead, the drone live at its height and heading — and the mission
 * controller's progress. While the mission flies the start stays where it took
 * off. The flow is locked here while it flies: a plan cannot be edited in the
 * air. Land and Emergency stop stay in the strip at the top, as always.
 *
 * THE DPP SWITCH sits beside Start (on by default in Auto), and when the
 * mission has landed its verdicts appear under the progress (DataPipeline).
 */

import { useEffect, useRef, useState } from "react";
import type { History, Run } from "@/App";
import {
  api, AgentError, type FlyingPlan, type MissionBlocker, type MissionProgress, type MissionView,
  type RoomView, type Session, type Telemetry, type XY,
} from "@/lib/agent";
import { formatDuration } from "@/lib/format";
import { Button, Message, Panel, Spinner, StatusDot, type Tone } from "@/components/ui";
import { Field } from "../ControlPage";
import { FlightResults } from "../DataPipeline";
import { checkComplete } from "./CheckStep";
import { dronePosition } from "./plan/MissionStep";
import { flownPoints, landsAt, returnsHome, type PathSource } from "./plan/path";
import { RoomMap, type PointState } from "./plan/RoomMap";

const TERMINAL: MissionProgress["state"][] = ["done", "aborted", "interrupted", "failed"];

const OUTCOME: Record<string, { tone: Tone; text: string }> = {
  done: { tone: "good", text: "Mission complete — every inspection point was held and recorded." },
  aborted: { tone: "warning", text: "The mission ended early and the drone landed. The last event says why." },
  interrupted: { tone: "warning", text: "You took the drone back with the keys. It is in your hands, as in Manual." },
  failed: { tone: "critical", text: "The mission controller hit an error and the flight was ended. The agent's log has the details." },
};

/** Each point's state while this mission flies, for the room map. */
export function progressOf(mission: MissionView, progress: MissionProgress | null): Record<string, PointState> {
  const done = new Set(progress?.id === mission.id ? progress.completed_point_ids : []);
  const current = progress?.id === mission.id ? progress.current_point_id : null;
  return Object.fromEntries(mission.points.map((p) => [
    p.id, done.has(p.id) ? "done" : p.id === current ? "current" : "pending",
  ]));
}

/** The mission as it will fly from `start` — Mission.from_start, drawn: the
 *  start moved, the points kept, the end point applied. */
/** The plan as it will be flown from `start` — the whole path moves with it,
 *  as the agent's Mission.from_start does (2026-10-05). Shown only until the
 *  agent's own auto-corrected plan arrives. */
export function fromStart(mission: MissionView, start: XY): PathSource {
  const dx = start[0] - mission.home[0];
  const dy = start[1] - mission.home[1];
  return {
    home: start,
    points: flownPoints(mission).map((p) => ({ ...p, x_m: p.x_m + dx, y_m: p.y_m + dy })),
    return_to_start: returnsHome(mission), end_point_id: null,
  };
}

/** How often the agent is asked for the plan and the reasons, before takeoff. */
const ASK_EVERY_MS = 2000;

export function FlyStep({ session, run, telemetry, mission, ambient, setAmbient, onPlanAnother, heightClass }: {
  heightClass?: string;
  session: Session;
  run: Run;
  telemetry: Telemetry | null;
  /** Kept for the flow's signature; the room map reads the telemetry itself. */
  history?: History;
  mission: MissionView;
  ambient: string;
  setAmbient: (value: string) => void;
  onPlanAnother: () => void;
}) {
  const [room, setRoom] = useState<RoomView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setError(null);
    api.room(mission.room_id)
      .then((r) => { if (live) setRoom(r); })
      .catch((e: unknown) => { if (live) setError(e instanceof AgentError ? e.message : "The room could not be read."); });
    return () => { live = false; };
  }, [mission.room_id, attempt]);

  const progress = session.mission?.id === mission.id ? session.mission : null;
  const flying = session.activity === "mission";
  const finished = progress !== null && TERMINAL.includes(progress.state) && !flying;
  // End session shows exactly when the flight strip's does (FlightStrip.tsx):
  // in a session, nothing in the air (App.tsx `flying`), not already ending.
  const airborne = ["manual", "program", "mission"].includes(session.activity ?? "");
  const canEnd = !["signed_out", "idle"].includes(session.state) && !airborne
    && session.state !== "ending";
  const drone = dronePosition(telemetry);
  // Where this flight took off: fixed for the flight, so the start does not
  // follow the drone round the room.
  const [tookOff, setTookOff] = useState<XY | null>(null);
  const wasFlying = useRef(false);
  useEffect(() => {
    if (flying && !wasFlying.current) setTookOff(drone);
    wasFlying.current = flying;
  }, [flying, drone]);
  const start = (flying || finished) ? tookOff ?? drone : drone;

  // The agent's auto-corrected plan and every reason it would refuse, asked
  // every 2 s until takeoff; while flying the last plan stays on the map.
  const [agentPlan, setAgentPlan] = useState<FlyingPlan | null>(null);
  const [blockers, setBlockers] = useState<MissionBlocker[] | null>(null);
  const [askError, setAskError] = useState<string | null>(null);
  useEffect(() => {
    if (flying) return;
    let live = true;
    const ask = () => {
      api.missionBlockers(mission.id)
        .then((b) => { if (live) { setBlockers(b.blockers); setAskError(null); } })
        .catch((e: unknown) => { if (live) setAskError(e instanceof AgentError ? e.message : "Could not ask the agent whether the mission can start."); });
      api.missionFromDrone(mission.id)
        .then((p) => { if (live) setAgentPlan(p); })
        .catch(() => { if (live) setAgentPlan(null); });
    };
    ask();
    const timer = window.setInterval(ask, ASK_EVERY_MS);
    return () => { live = false; window.clearInterval(timer); };
  }, [mission.id, flying, session.state, session.assisted]);
  const path: PathSource = agentPlan?.position ? agentPlan.mission
    : start ? fromStart(mission, start) : mission;
  const lands = landsAt(mission);

  const reason = (): string | null => {
    if (flying) return "The mission is flying.";
    if (!mission.valid) return "This mission has problems to fix first (step ①).";
    if (session.state === "idle" || session.state === "signed_out") return "Start the session first (step ⑤).";
    if (session.state === "starting") return "The checks are still running.";
    if (session.state === "checks_failed") return "The checks did not pass (step ⑤).";
    if (session.state === "awaiting_confirmation") return "Confirm the area is clear first (step ⑤).";
    if (session.retry_required) return "Retry the checks — the last flight ended early (step ⑤).";
    if (session.state === "busy") return "The drone is busy. Wait for it to finish.";
    if (!checkComplete(session)) return "The session is not ready.";
    if (!session.assisted) return `The drone does not know where it is, so it cannot fly a mission. ${session.unassisted_reason ?? ""} Measure it in ② Position, then Check again.`;
    if (blockers && blockers.length) return blockers[0].message;
    return null;
  };
  const why = reason();

  return (
    <div className="grid gap-3">
      <section aria-label="Start the mission" className="flex flex-wrap items-end justify-between gap-3 border border-[var(--border)] bg-[var(--surface)] p-3">
        <div className="min-w-0">
          <p className="eyebrow">Mission · revision {mission.revision}</p>
          <h2 className="wrap-anywhere text-lg font-bold text-[var(--heading)]">{mission.name}</h2>
          <p className="mono text-xs text-[var(--muted)]">
            {flownPoints(mission).length} point{flownPoints(mission).length === 1 ? "" : "s"} · about {formatDuration(mission.estimated_duration_s)} · {returnsHome(mission) ? "returns to where it took off" : lands ? `lands at ${lands}` : "no points"}
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-28">
            <Field label="Room temp" value={ambient} onChange={setAmbient} hint="74F or 22C" />
          </div>
          <div className="grid gap-1">
            <Button
              variant="primary"
              disabled={why !== null}
              title={why ?? undefined}
              onClick={() => void run(() => api.runMission(mission.id, ambient), `Start mission ${mission.name}`)}
            >
              {flying ? "Flying…" : "Start mission"}
            </Button>
          </div>
        </div>
        {/* Why Start is disabled is the one thing the operator must read here:
            a warning, never muted text (2026-10-01 — a refusal in grey read as
            "the mission is running"). */}
        {!flying && blockers && blockers.length > 0 && (
          <div className="grid w-full gap-2" role="status" aria-label="Why the mission cannot start">
            <p className="text-sm font-semibold">The mission cannot start yet:</p>
            <ul className="grid gap-2">
              {blockers.map((b) => (
                <li key={b.code} className="grid gap-1 border-l-2 border-[var(--status-warning)] pl-3 text-sm">
                  <span>{b.message}</span>
                  <span className="text-xs">→ {b.fix}</span>
                  {b.code === "motors" && (
                    <span><Button onClick={() => void run(api.resetDrone, "Reset drone")}>Reset drone</Button></span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        {!flying && (!blockers || blockers.length === 0) && why && <div className="w-full"><Message tone="warning" text={why} /></div>}
        {!flying && askError && <div className="w-full"><Message tone="critical" text={askError} /></div>}
        {flying && (
          <p className="w-full text-xs">
            Use <strong>Land</strong> at the top (or <kbd>L</kbd>) to bring it down now. Any movement key takes the drone back from the mission.
          </p>
        )}
      </section>

      {finished && progress && (
        <div className="grid gap-2">
          <Message tone={OUTCOME[progress.state]?.tone ?? "idle"} text={OUTCOME[progress.state]?.text ?? progress.state} />
          {canEnd && (
            // The flight is already saved and queued when it lands
            // (Session._finish_flight) — ending adds the session's own wrap-up,
            // so the line must not say the flight waits for it.
            <p className="text-sm">This flight is saved and uploading. End the session when you're done flying.</p>
          )}
          <div className="flex flex-wrap gap-2">
            {canEnd && (
              <Button variant="primary" onClick={() => void run(api.endSession, "End session")}>End session</Button>
            )}
            <Button onClick={onPlanAnother}>Plan or pick another mission</Button>
          </div>
        </div>
      )}

      <div className="@container">
      <div className="grid gap-3 @3xl:grid-cols-[minmax(0,1.6fr)_minmax(14rem,1fr)]">
        <div className="min-w-0">
          {error ? (
            <div className="grid gap-3 border border-[var(--border)] p-4">
              <Message tone="critical" text={error} />
              <div><Button onClick={() => setAttempt((n) => n + 1)}>Try again</Button></div>
            </div>
          ) : room ? (
            <RoomMap
              outer={room.outer} fence={room.geofence} obstacles={room.obstacles}
              path={path} takeoffHeight={mission.cruise_height_m}
              drone={drone} droneHeight={telemetry?.height_m ?? null}
              droneYaw={telemetry?.values["stabilizer.yaw"] ?? null}
              progress={progressOf(mission, progress)} heightClass={heightClass}
              label={`${mission.name}, from where the drone is: the room, the path and the drone`}
            />
          ) : (
            <div className="border border-[var(--border)] p-4"><Spinner label="Loading the room…" /></div>
          )}
          {!flying && start && (
            <p className="pt-1 text-xs">The path starts from where the drone is (D): the whole plan moves with it, fitted into the green.</p>
          )}
        </div>

        <Panel
          title="Progress"
          action={progress
            ? <StatusDot tone={progress.state === "done" ? "good" : TERMINAL.includes(progress.state) ? "warning" : "idle"}>{progress.state.replace("_", " ")}</StatusDot>
            : <StatusDot tone="idle">not started</StatusDot>}
          bodyClassName="grid gap-2 px-4 py-3"
        >
          <ol className="grid gap-1">
            {flownPoints(mission).map((p) => {
              const done = progress?.completed_point_ids.includes(p.id) ?? false;
              const current = progress?.current_point_id === p.id;
              return (
                <li key={p.id} className="grid gap-0.5">
                  <span className="mono flex flex-wrap items-baseline gap-2 text-xs">
                    <span aria-hidden="true" className="w-4 text-center"
                          style={{ color: done || current ? "var(--primary)" : "var(--muted)" }}>
                      {done ? "✓" : current ? "●" : "○"}
                    </span>
                    <span className="font-semibold">{p.id}</span>
                    <span className="min-w-0 wrap-anywhere text-[var(--muted)]">{p.label ?? ""}</span>
                    <span className="sr-only">{done ? " — done" : current ? " — holding here now" : " — ahead"}</span>
                  </span>
                </li>
              );
            })}
          </ol>
          {progress?.last_event && (
            <p className="border-t border-[var(--border)] pt-2 text-xs">{progress.last_event.detail}</p>
          )}
        </Panel>
      </div>
      </div>

      {(finished || session.processing?.last_flight_id) && !flying && (
        <Panel title="Results" note="The data pipeline's verdict for each inspection point of the last flight." bodyClassName="grid gap-2 px-4 py-3">
          <FlightResults session={session} run={run} />
        </Panel>
      )}
    </div>
  );
}
