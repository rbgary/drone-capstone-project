// This file was modified on 11/20/2026 with the assistance of Claude Code in VS Code
// (Claude Opus 5.5, Anthropic, 2026). The mission list takes an `aside`, shown
// above the list only, where step ① puts the Hover test.

/**
 * Step ① of the Auto flow: choose the mission to fly.
 *
 *   list     every mission saved on this laptop — ready to fly, or what to fix
 *            — with New mission, and Edit on every row
 *   view     one mission: its plan drawn, its points, the agent's check;
 *            Edit, Delete, or Use this mission
 *   edit     the room and the path (MissionEditor) — a new mission and a saved
 *            one open the same editor
 *
 * THE LIST IS HOME (2026-09-29, the owner's ask). Leaving the editor — "← All
 * missions", Close, or Save — always comes back to the list, never to one
 * mission's page: Save lands on the list with "Saved …" and the mission
 * highlighted, so the next thing (New mission, Edit another) is one click.
 * Leaving with unsaved changes asks first, in the editor.
 *
 * "Use this mission" is what completes the step; the flow then moves to ②.
 * Every list has its four states: loading, empty, an error with a retry, and
 * content (desktop design rule, and pages-and-windows.txt).
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import type { Run } from "@/App";
import {
  api, AgentError, type MissionView, type PlanLimits, type RoomView, type Telemetry, type XY,
} from "@/lib/agent";
import { formatDateTime, formatDuration, formatMetres } from "@/lib/format";
import { Button, Message, Panel, SkeletonPanel, StatusDot } from "@/components/ui";
import { SmallButton } from "./fields";
import { mapBox, placeOf } from "./geometry";
import { blankMission, blankRoom, MissionEditor, outerOf, ProblemList, type Draft } from "./MissionEditor";
import { landsAt, returnsHome, unflownIds } from "./path";
import { RoomMap } from "./RoomMap";
import { spaceOf, type SpaceLayer } from "./space";

type View = { kind: "list" } | { kind: "view"; id: string } | { kind: "edit"; draft: Draft };

type Loaded = { missions: MissionView[]; rooms: RoomView[]; limits: PlanLimits };

/** Where the drone is — only when the agent says x and y are a place. A still
 *  drone with no base station measured once read 92 m from the start, and
 *  climbing: the estimate drifting, which is a number, not a position. */
export function dronePosition(telemetry: Telemetry | null): XY | null {
  if (telemetry?.positioned !== true) return null;
  const x = telemetry?.values["stateEstimate.x"];
  const y = telemetry?.values["stateEstimate.y"];
  return x === undefined || y === undefined ? null : [x, y];
}

export function MissionStep({ run, telemetry, selectedId, onUse, openEditor, heightClass, aside }: {
  run: Run;
  telemetry: Telemetry | null;
  selectedId: string | null;
  onUse: (mission: MissionView) => void;
  /** Harness only: open this mission's editor once loaded. */
  openEditor?: string;
  /** The room map's height — taller in full screen. */
  heightClass?: string;
  /** Shown above the list, on the list only — the list is home, so this is
   *  what the step offers besides a mission (the Hover test, 2026-11-20).
   *  Never shown while one mission is open or being edited. */
  aside?: ReactNode;
}) {
  const [view, setView] = useState<View>({ kind: "list" });
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** The mission just saved: said on the list, and its row highlighted. */
  const [justSaved, setJustSaved] = useState<{ id: string; name: string; revision: number } | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [missions, rooms, limits] = await Promise.all([api.missions(), api.rooms(), api.planLimits()]);
      setLoaded({ missions, rooms, limits });
    } catch (e) {
      setError(e instanceof AgentError ? e.message : "The saved missions could not be read.");
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  // The green (where the base station reaches) on the editor's map, so a
  // point drawn outside it is ringed red while it is drawn (2026-10-05).
  const [space, setSpace] = useState<SpaceLayer | null>(null);
  const editingRoom = view.kind === "edit" && !view.draft.roomIsNew ? view.draft.room.id : null;
  useEffect(() => {
    if (!editingRoom) { setSpace(null); return; }
    let live = true;
    api.roomCoverage(editingRoom).then((c) => { if (live) setSpace(spaceOf(c)); })
      .catch(() => { if (live) setSpace(null); });
    return () => { live = false; };
  }, [editingRoom]);
  const [opened, setOpened] = useState(false);
  useEffect(() => {
    if (!openEditor || opened || !loaded) return;
    const mission = loaded.missions.find((m) => m.id === openEditor);
    const room = mission && loaded.rooms.find((r) => r.id === mission.room_id);
    if (mission && room) {
      setView({ kind: "edit", draft: { room, mission, roomIsNew: false, missionIsNew: false } });
      setOpened(true);
    }
  }, [openEditor, opened, loaded]);

  const drone = dronePosition(telemetry);

  if (error) {
    return (
      <div className="grid content-start gap-3">
        {aside}
        <Panel title="Missions">
          <div className="grid gap-3">
            <Message tone="critical" text={error} />
            <div><Button onClick={() => void load()}>Try again</Button></div>
          </div>
        </Panel>
      </div>
    );
  }
  if (!loaded) return <SkeletonPanel lines={4} />;

  const { missions, rooms, limits } = loaded;
  const roomOf = (id: string) => rooms.find((r) => r.id === id) ?? null;

  if (view.kind === "edit") {
    const others = missions.filter((m) => m.room_id === view.draft.room.id && m.id !== view.draft.mission.id).length;
    return (
      <MissionEditor
        draft={view.draft} limits={limits} run={run} drone={drone} missionsInRoom={others} heightClass={heightClass}
        space={space}
        onCancel={() => { setJustSaved(null); setView({ kind: "list" }); }}
        onSaved={(saved) => {
          void load();
          setJustSaved({ id: saved.id, name: saved.name, revision: saved.revision });
          setView({ kind: "list" });
        }}
      />
    );
  }

  const edit = (mission: MissionView) => {
    const room = roomOf(mission.room_id);
    if (!room) return;
    setJustSaved(null);
    setView({ kind: "edit", draft: { room, mission, roomIsNew: false, missionIsNew: false } });
  };

  if (view.kind === "view") {
    const mission = missions.find((m) => m.id === view.id);
    if (!mission) {
      return (
        <Panel title="Mission">
          <div className="grid gap-3">
            <Message tone="warning" text="That mission is no longer on this computer." />
            <div><Button onClick={() => setView({ kind: "list" })}>Back to the missions</Button></div>
          </div>
        </Panel>
      );
    }
    return (
      <MissionDetail
        mission={mission} room={roomOf(mission.room_id)} limits={limits} drone={drone} run={run} heightClass={heightClass}
        selected={selectedId === mission.id}
        onBack={() => setView({ kind: "list" })}
        onEdit={(room) => setView({ kind: "edit", draft: { room, mission, roomIsNew: false, missionIsNew: false } })}
        onDeleted={() => { void load(); setView({ kind: "list" }); }}
        onUse={() => onUse(mission)}
      />
    );
  }

  return (
    <div className="grid content-start gap-3">
      {aside}
      <MissionList
        missions={missions} rooms={rooms} limits={limits} selectedId={selectedId} justSaved={justSaved}
        onView={(id) => { setJustSaved(null); setView({ kind: "view", id }); }}
        onEdit={edit}
        onNew={(room) => {
          setJustSaved(null);
          const fresh = room ?? blankRoom(limits);
          setView({ kind: "edit", draft: {
            room: fresh, mission: blankMission(fresh, limits, drone), roomIsNew: room === null, missionIsNew: true,
          } });
        }}
      />
    </div>
  );
}

function MissionList({ missions, rooms, selectedId, justSaved, onView, onEdit, onNew }: {
  missions: MissionView[];
  rooms: RoomView[];
  limits: PlanLimits;
  selectedId: string | null;
  justSaved: { id: string; name: string; revision: number } | null;
  onView: (id: string) => void;
  onEdit: (mission: MissionView) => void;
  onNew: (room: RoomView | null) => void;
}) {
  const hasRoom = (id: string) => rooms.some((r) => r.id === id);
  const [roomChoice, setRoomChoice] = useState<string>(rooms[0]?.id ?? "");
  const roomName = (id: string) => rooms.find((r) => r.id === id)?.name ?? "a missing room";

  return (
    <Panel
      title="Saved missions"
      note="A mission is a floor plan — the room's geofence and obstacles — and the inspection points the drone flies to, in order. Pick one to fly, or plan a new one."
      bodyClassName="grid gap-3 p-4"
    >
      <div className="flex flex-wrap items-end gap-2 border-b border-[var(--border)] pb-3">
        <label className="grid gap-1 text-xs">
          <span className="eyebrow">Plan a new mission in</span>
          <select
            value={roomChoice}
            onChange={(e) => setRoomChoice(e.target.value)}
            className="min-h-9 border border-[var(--border)] bg-[var(--surface-2)] px-2 text-sm"
          >
            {rooms.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            <option value="">A new room</option>
          </select>
        </label>
        <Button variant="primary" onClick={() => onNew(rooms.find((r) => r.id === roomChoice) ?? null)}>
          New mission
        </Button>
        <p className="w-full text-xs text-[var(--muted)]">
          Rooms are drawn inside the agent&apos;s flying area (±2 m). The measured flyable space never changes your plan — ② Check shows the plan that will fly inside it.
        </p>
      </div>

      {justSaved && (
        <Message tone="good" text={`Saved “${justSaved.name}” (revision ${justSaved.revision}). Edit it again, plan a new one, or pick one to fly.`} />
      )}

      {missions.length === 0 ? (
        <p className="text-sm">No missions yet. Plan the first one above: draw the room, then place the points.</p>
      ) : (
        <ul className="grid gap-2">
          {missions.map((m) => {
            const errors = m.problems.filter((p) => p.severity === "error").length;
            const saved = justSaved?.id === m.id;
            return (
              <li key={m.id} className={`flex items-stretch border border-l-4 bg-[var(--surface)] ${
                saved ? "border-[var(--status-good)]"
                : selectedId === m.id ? "border-[var(--border)] border-l-[var(--primary)]"
                : "border-[var(--border)] border-l-[var(--border)]"}`}>
                <button
                  type="button"
                  onClick={() => onView(m.id)}
                  title="Open this mission: its plan, Use it, or Delete it"
                  className="grid min-w-0 flex-1 gap-1 px-3 py-2 text-left hover:bg-[var(--surface-2)]"
                >
                  <span className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="wrap-anywhere text-sm font-semibold">{m.name}</span>
                    <span className="text-xs">
                      {m.valid
                        ? <StatusDot tone="good">Ready to fly</StatusDot>
                        : <StatusDot tone="critical">{`${errors} to fix`}</StatusDot>}
                    </span>
                  </span>
                  <span className="mono text-xs text-[var(--muted)]">
                    {roomName(m.room_id)} · {m.points.length} point{m.points.length === 1 ? "" : "s"} · about {formatDuration(m.estimated_duration_s)} · revision {m.revision}
                    {m.flown_revision !== null ? ` · flown r${m.flown_revision}` : " · not flown"}
                    {selectedId === m.id ? " · selected" : ""}
                    {saved ? " · just saved" : ""}
                  </span>
                </button>
                {/* Edit straight from the list — the same editor a new
                    mission opens in; saving brings you back here. */}
                <div className="flex shrink-0 items-center border-l border-[var(--border)] px-2">
                  <SmallButton onClick={() => onEdit(m)} disabled={!hasRoom(m.room_id)}
                               title={hasRoom(m.room_id) ? `Edit ${m.name}` : "Its room is missing, so there is nothing to draw it in."}>
                    Edit
                  </SmallButton>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

function MissionDetail({ mission, room, limits, drone, run, selected, onBack, onEdit, onDeleted, onUse, heightClass }: {
  heightClass?: string;
  mission: MissionView;
  room: RoomView | null;
  limits: PlanLimits;
  drone: XY | null;
  run: Run;
  selected: boolean;
  onBack: () => void;
  onEdit: (room: RoomView) => void;
  onDeleted: () => void;
  onUse: () => void;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);
  const outer = room ? outerOf(room, limits) : limits.outer;
  const box = mapBox(outer);
  const unflown = unflownIds(mission);
  const lands = landsAt(mission);
  const [startLeft, startFront] = placeOf(mission.home, box);
  const blocking = mission.problems.filter((p) => p.severity === "error");

  return (
    <div className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <SmallButton onClick={onBack}>← All missions</SmallButton>
          <h2 className="wrap-anywhere pt-2 text-lg font-bold text-[var(--heading)]">{mission.name}</h2>
          <p className="mono text-xs text-[var(--muted)]">
            {room?.name ?? "Room missing"} · revision {mission.revision} · saved {formatDateTime(mission.updated_at)}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {confirmDelete ? (
            <>
              <span className="text-xs">Delete this mission?</span>
              <SmallButton tone="danger" onClick={() => void run(async () => {
                await api.deleteMission(mission.id);
                onDeleted();
              }, `Delete mission ${mission.name}`)}>Delete</SmallButton>
              <SmallButton onClick={() => setConfirmDelete(false)}>Keep it</SmallButton>
            </>
          ) : (
            <SmallButton tone="danger" onClick={() => setConfirmDelete(true)}>Delete</SmallButton>
          )}
          <SmallButton disabled={!room} onClick={() => room && onEdit(room)}
                       title={room ? undefined : "Its room is missing, so there is nothing to draw it in."}>
            Edit
          </SmallButton>
          <Button
            variant="primary"
            disabled={!mission.valid}
            title={mission.valid ? undefined : "Fix what the agent's check lists first."}
            onClick={onUse}
          >
            {selected ? "Selected — continue →" : "Use this mission →"}
          </Button>
        </div>
      </div>
      {!mission.valid && (
        <Message tone="critical" text={`This mission cannot fly until ${blocking.length === 1 ? "one problem is" : `${blocking.length} problems are`} fixed. Edit it, or pick another.`} />
      )}

      <div className="@container">
      <div className="grid gap-3 @4xl:grid-cols-[minmax(0,1.3fr)_minmax(18rem,1fr)]">
        <div className="min-w-0">
          <RoomMap
            outer={outer} fence={room?.geofence ?? null} obstacles={room?.obstacles ?? []}
            path={mission} takeoffHeight={mission.cruise_height_m}
            problems={mission.problems} drone={drone} heightClass={heightClass}
            label={`The room map of ${mission.name}`}
          />
        </div>
        <div className="grid content-start gap-3">
          <Panel title="Inspection points" bodyClassName="p-0">
            <table className="w-full text-xs">
              <thead className="text-left">
                <tr className="border-b border-[var(--border)]">
                  {["Point", "From left, front", "Height", "Hold"].map((h) => (
                    <th key={h} scope="col" className="eyebrow px-3 py-2 font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="mono">
                {mission.points.map((p) => (
                  <tr key={p.id} className={`border-b border-[var(--border)] last:border-0 ${unflown.has(p.id) ? "opacity-60" : ""}`}>
                    <td className="px-3 py-1.5 font-semibold">
                      {p.id}{p.label ? ` · ${p.label}` : ""}{lands === p.id ? " · END" : ""}{unflown.has(p.id) ? " · not flown" : ""}
                    </td>
                    <td className="px-3 py-1.5">{placeOf([p.x_m, p.y_m], box).map((v) => v.toFixed(2)).join(", ")} m</td>
                    <td className="px-3 py-1.5">{p.z_m.toFixed(2)} m</td>
                    <td className="px-3 py-1.5">{p.hold_s.toFixed(0)} s</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mono px-3 py-2 text-xs text-[var(--muted)]">
              Planned start {startLeft.toFixed(2)}, {startFront.toFixed(2)} m · takeoff {formatMetres(mission.cruise_height_m)} · {returnsHome(mission) ? "returns to the start" : lands ? `lands at ${lands}` : "no points"} · the flight starts from wherever the drone is
            </p>
          </Panel>
          <ProblemList problems={mission.problems} error={null} checking={false} summary={mission} />
        </div>
      </div>
      </div>
    </div>
  );
}
