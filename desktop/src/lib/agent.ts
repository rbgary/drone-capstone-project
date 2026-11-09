/**
 * Talking to the flight agent running on this computer.
 *
 * The contract is the agent's own, from `backend/agent/dronedeck/api/rest.py`:
 * REST for commands, one WebSocket for everything live. Every command carries
 * the control token, which the Tauri shell hands us — a page on the internet
 * cannot know it, so it cannot fly the drone.
 *
 * **This file never generates a setpoint stream.** It reports which keys are
 * held and pings ~10 times a second; the agent runs the 50 Hz loop and lands
 * the drone by itself if this window goes quiet.
 */

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export type Mode = "auto" | "manual";

export type SessionState =
  | "signed_out" | "idle" | "starting" | "checks_failed"
  | "awaiting_confirmation" | "ready" | "busy" | "ending";

export type Check = {
  key: string;
  label: string;
  /** `warning` means flying is allowed with less help from the drone. */
  status: "running" | "passed" | "warning" | "failed";
  detail: string;
  data: Record<string, unknown>;
};

export type Session = {
  state: SessionState;
  mode: Mode;
  operator: { id: string; email: string; name: string | null; role?: string } | null;
  drone: { hardware_id: string; battery_v: number; endurance_s: number } | null;
  session_id: string | null;
  activity: string | null;
  checks: Check[];
  health_test: HealthTest | null;
  flight: { id: string; phase: string; detail: string } | null;
  message: string | null;
  can_fly: boolean;
  /** True until the agent has tried the saved sign-in. Show a loading state,
   *  never a sign-in form: "signed out" is not known to be true yet. */
  restoring: boolean;
  /** False when the drone has no usable position estimate: no height hold, no
   *  programs, and the position guards cannot run. */
  assisted: boolean;
  unassisted_reason: string | null;
  /** The drone reported its AI deck earlier and now does not: the decks lost
   *  power or contact. Only switching it off and on brings them back. */
  decks_lost?: boolean;
  /** The last flight ended abnormally. Nothing flies until Retry has run every
   *  check again in this session — after a tumble the drone holds its motors. */
  retry_required: boolean;
  /** The radio, apart from any session: signed in and idle, the agent holds
   *  the drone on standby — vitals and the camera, never the motors. */
  radio: Radio;
  /** The mission being flown, or the last one flown this session; null when
   *  none has run. From the agent's session snapshot (session.py). */
  mission: MissionProgress | null;
  /** The DPP switch (dronedeck/processing.py): whether the next flight is
   *  processed when it lands, whether that is the operator's choice or still
   *  the mode's default (on in Auto, off in Manual), and recent jobs. */
  processing: Processing;
  /** The auto-capture switch (agent camera/recording.py): `auto` — frames are
   *  recorded automatically; off, only manual captures are saved. Absent from
   *  older agents. */
  capture?: { auto: boolean; chosen: boolean };
  /** What the session is for: "flight" (Control) or "capture" (the Capture
   *  page, which never flies). Absent from older agents: "flight". */
  purpose?: "flight" | "capture";
  /** Which way the arrow keys move the drone (dronedeck/flight/keyframe.py):
   *  the operator's choice, their marked spot, and — while flying — what the
   *  arrows mean right now. */
  controls: Controls;
};

/** "operator": away from you. "room": the room's own directions. */
export type KeyFrameChoice = "operator" | "room";


/** What the arrows mean on this tick, from the agent. */
export type ArrowFrame = {
  chosen: KeyFrameChoice;
  /** "takeoff": no position — the direction the nose had at takeoff.
   *  "nose": not even a heading — the old behaviour. */
  active: "operator" | "room" | "takeoff" | "nose";
  reason: "near_operator" | "no_position" | "no_heading" | null;
  /** The room direction ↑ moves the drone, degrees from +x; null for "nose". */
  forward_deg: number | null;
  operator: [number, number] | null;
  operator_source: "marked" | "takeoff" | null;
};

export type Controls = {
  key_frame: KeyFrameChoice;
  /** The operator's marked spot, room metres; null: the takeoff spot is used. */
  operator: [number, number] | null;
  operator_marked_at: string | null;
  /** null on the ground. */
  live: ArrowFrame | null;
};

export type ProcessingJob = {
  /** The flight's id — or the session's, for a session job. */
  flight_id: string;
  state: "queued" | "running" | "done" | "failed";
  queued_at: string;
  finished_at: string | null;
  error: string | null;
  /** "flight", "session" (the session around its flights) or "live" (one
   *  point's verdict in flight, story 4.9). Absent from an older agent. */
  kind?: "flight" | "session" | "live";
  point_id?: string | null;
};

/** One inspection point's verdict while the mission still flies (story 4.9,
 *  agent pipeline/runner.py run_live_point). */
export type LivePoint = {
  state: ProcessingJob["state"];
  verdict?: "normal" | "anomaly" | "insufficient_data";
  reasons?: string[];
  findings?: { id: string; title: string; severity: "info" | "warning" | "critical"; sentence: string; signal: string }[];
  error?: string | null;
};

export type Processing = {
  on: boolean;
  chosen: boolean;
  jobs: ProcessingJob[];
  /** The session's most recent flight to land — processed or not. */
  last_flight_id: string | null;
  /** The flight in the air's points, judged as each hold ends. */
  live?: { flight_id: string | null; points: Record<string, LivePoint> };
};

/** One inspection point's verdict (pipeline/contracts.py PointResult). */
export type PointVerdict = {
  point_id: string;
  verdict: "normal" | "anomaly" | "insufficient_data";
  reasons: string[];
  alerts: { severity: "info" | "warning" | "critical"; message?: string; [key: string]: unknown }[];
};

/** One finding (pipeline/contracts.py Finding): a block of readings that
 *  departed from what was expected, judged and put into words. */
export type PipelineFinding = {
  id: string;
  signal: "temperature" | "pressure";
  severity: "info" | "warning" | "critical";
  title: string;
  sentence: string;
  t_start_s: number;
  t_end_s: number;
  point_ids: string[];
  evidence_frames: number[];
  image_support: "supports" | "contradicts" | "cannot_tell";
  image_note: string;
};

/** results/<flight id>/result.json, as the pipeline wrote it. Version 1
 *  results (before 2026-10-09) have no findings — every v2 field is optional
 *  so a result already on disk still reads. */
export type FlightResult = {
  flight_id: string;
  job: ProcessingJob | null;
  result: {
    flight_id: string;
    pipeline_version: string;
    stages: Record<string, string>;
    created_at: string;
    points: PointVerdict[];
    failures: { point_id: string; stage: string; reason: string }[];
    summary: Record<string, { readings?: number; frames?: number; flags?: number }>;
    findings?: PipelineFinding[];
  };
};

/** What the mission controller reports while a mission flies. */
export type MissionProgress = {
  id: string;
  name: string;
  revision: number;
  room_id: string;
  points: string[];
  state: "idle" | "taking_off" | "transit" | "arriving" | "holding" | "returning"
    | "landing" | "done" | "aborted" | "interrupted" | "failed";
  current_point_id: string | null;
  completed_point_ids: string[];
  last_event: { kind: string; at_s: number; point_id: string | null; detail: string } | null;
};

// ── rooms and missions (backend/agent/dronedeck/mission/plan) ─────────
//
// Metres, in the Lighthouse room frame: x and y absolute, heights above the
// floor captured at takeoff. Feet appear only in labels (format.ts).

export type XY = [number, number];

export type Problem = {
  code: string;
  message: string;
  severity: "error" | "warning";
  /** A point id, "home", a leg ("P1 → P2"), an obstacle id, or null. */
  where: string | null;
};

export type FenceShape = "rectangle" | "circle" | "polygon";

export type Geofence = {
  shape: FenceShape;
  vertices: XY[];
  z_min: number;
  z_max: number;
};

export type ObstacleKind = "line" | "rectangle" | "circle";

export type Obstacle = {
  id: string;
  kind: ObstacleKind;
  label: string | null;
  /** line: two ends · rectangle: two opposite corners · circle: the centre. */
  points: XY[];
  radius?: number;
  /** Height above the floor, for the room map; null = floor to ceiling. The
   *  agent's checks treat every obstacle as floor to ceiling regardless. */
  height_m?: number | null;
};

/** The largest area anything in a room may occupy: its measured Lighthouse
 *  coverage, or the agent's default area until that is measured. */
export type OuterBound = { vertices: XY[]; measured: boolean };

/** A point the fit moved into the flyable space (mission/plan/fit.py). */
export type PlanMove = { point_id: string; from: [number, number, number]; to: [number, number, number]; distance_m: number };

/** THE PLAN THAT WILL FLY — exactly what Start flies (GET …/from-drone, POST /missions/fit). */
export type FlyingPlan = {
  position: XY | null;
  mission: MissionView;
  /** The room's fence clipped to where the position can be trusted. */
  space: Geofence;
  moves: PlanMove[];
  unfitted: string[];
};

/** One height's predicted outline. */
export type CoverageSlice = { z_m: number; outline: XY[] };

export type SurveyStatus =
  | { active: false }
  | { active: true; room_id: string; seen: number; kept: number; spots: number; outline: XY[] };

/** The base station as the drone sees it (Auto › ② Position, Set up step 3). */
/** GET /station/space — where the measured stations' light reaches, with no
 *  room: Manual's map. `predicted` null: no drone, or no station measured. */
export type StationSpace = {
  predicted: RoomCoverage["predicted"];
  /** The agent's flying area: a square this far each way from the origin. */
  fence_half_extent_m: number;
};

export type StationStatus =
  | { connected: false; measuring: boolean }
  | {
      connected: true;
      /** A measurement is reading the drone right now. */
      measuring: boolean;
      /** The chain from light to position, stage by stage: how many of the
       *  deck's four sensors see light, which stations' data was read. */
      light_sensors: number;
      calibrated: number[];
      /** Calibration, stored place and live sweeps together: in use (bsActive). */
      active: number[];
      /** Sweeps decoded into angles — the firmware sets bsReceive only then. */
      received: number[];
      measured: number[];
      usable: number[];
      uncertainty_cm: number | null;
      ready: boolean;
      /** The same chain per station, named by channel (station id + 1).
       *  Absent from older agents. */
      stations?: StationRow[];
      /** The positioning deck itself: detected at power-on, and whether its
       *  firmware needs writing. null = not known. Absent from older agents. */
      deck?: { detected: boolean | null; needs_fw: boolean | null };
      /** The FIRST stage from deck to position that is not done, with its fix
       *  (agent flight/position_stages.py); null when the drone is placed. */
      blocker?: PositionBlocker | null;
    };

export type PositionBlocker = {
  stage: "deck" | "firmware" | "light" | "decoded" | "stored" | "settled";
  text: string;
  fix: string;
};

/** A measured base station on the room map: its place in room metres and
 *  its own axes in room terms — x forward out of its face, y to its left, z
 *  up (agent mission/plan/coverage.py StationPose.axes). */
export type StationMarker = {
  id: number | null;
  /** The channel it is set to: the firmware's station number + 1. */
  channel: number | null;
  origin: [number, number, number];
  forward: [number, number, number];
  left: [number, number, number];
  up: [number, number, number];
};

export type StationRow = {
  id: number;
  channel: number;
  calibrated: boolean;
  received: boolean;
  measured: boolean;
  active: boolean;
};

/** One reason a mission cannot start now, and what to do about it. */
export type MissionBlocker = { code: string; message: string; fix: string };

/** A base station plugged into this laptop by USB (Bitcraze's "Set BS channel"). */
export type BaseStation = {
  port: string;
  serial_number: string | null;
  /** null: it did not answer. 0: a channel the drone cannot decode. */
  channel: number | null;
  supported: boolean;
};


/** Where the drone's position can be trusted: measured (flies) and predicted (a guide). */
export type RoomCoverage = {
  measured: Geofence | null;
  predicted: { z_min_m: number; z_max_m: number; slices: CoverageSlice[]; everywhere: Geofence | null } | null;
  survey: SurveyStatus;
};

export type Room = {
  format: number;
  id: string;
  name: string;
  geofence: Geofence;
  obstacles: Obstacle[];
  coverage: Geofence | null;
  clearance_m: number;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type RoomView = Room & { outer: OuterBound; problems: Problem[]; valid: boolean };

export type InspectionPoint = {
  id: string;
  label: string | null;
  x_m: number;
  y_m: number;
  z_m: number;
  hold_s: number;
};

export type Mission = {
  format: number;
  id: string;
  name: string;
  room_id: string;
  /** The PLANNED start. A flight starts from wherever the drone really is;
   *  this is what the plan is drawn and checked from until then. */
  home: XY;
  points: InspectionPoint[];
  cruise_height_m: number;
  return_to_start: boolean;
  /** Travel speed between points, m/s: 0.10, 0.15 or 0.20 (the default). */
  speed_m_s?: number;
  /** The point the flight ends and lands at; later points are not flown. */
  end_point_id?: string | null;
  revision: number;
  flown_revision: number | null;
  created_at: string;
  updated_at: string;
};

export type MissionView = Mission & {
  problems: Problem[];
  valid: boolean;
  path_length_m: number;
  estimated_duration_s: number;
};

export type PlanLimits = {
  outer: OuterBound;
  default_clearance_m: number;
  min_hold_s: number;
  z_min_m: number;
  z_max_m: number;
  move_speed_m_s: number;
  climb_rate_m_s: number;
};

export type Radio = {
  state: "off" | "searching" | "connected" | "paused" | "restarting";
  hardware_id: string | null;
  /** Why it is not connected, in words. */
  message: string | null;
};

/** The firmware's propeller test, then its battery test under load. */
export type HealthTest = {
  ok: boolean;
  motors: { passed: number[]; failed: number[]; ok: boolean };
  battery: {
    sag_v: number;
    passed: boolean;
    idle_vbat: number | null;
    /** What the drone reported for health.batTestPWMRatio; 0 = firmware default. */
    pwm_ratio: number | null;
  } | null;
  battery_error: string | null;
};

export type SyncStatus = {
  pending_flights: number;
  pending_events: number;
  uploading: string | null;
  last_error: string | null;
  everything_sent: boolean;
};

export type Telemetry = {
  values: Record<string, number>;
  height_m: number | null;
  /** x and y are a place: a station the drone can use and a tight Kalman
   *  spread (agent flight_guard.position_trusted). False = a number only. */
  positioned?: boolean;
  at: number;
};

/** Held-key state. Mirrors `Intent` in the agent, field for field. */
/** What the agent says about the camera. See backend camera/source.py. */
export type CameraStatus = {
  live: boolean;
  /** "none" | "test-pattern" | "ai-deck". */
  kind: string;
  reason: string | null;
  /** The DRONE's own answer (deck.bcAI). null until a drone has been asked,
   *  which is not the same as "not fitted". */
  deck_fitted: boolean | null;
  /** The deck was detected earlier and is not now — power-cycle, not "not fitted". */
  decks_lost?: boolean;
  /** The agent is actively working towards a picture (the drone is joining or
   *  rejoining Wi-Fi, or the camera is reconnecting): show progress, not "no
   *  signal". */
  connecting?: boolean;
  /** The size of the frames being served; null until one has arrived. */
  width: number | null;
  height: number | null;
  /** What actually arrives at the agent, over the last few seconds
   *  (camera/rate.py FrameRate). null without a deck camera. */
  rate?: CameraRate | null;
  /** What the GAP8 says each frame cost it, from the drone's console
   *  (camera/rate.py DeckTiming). null when it has said nothing lately. */
  deck_timing?: DeckTiming | null;
  /** What the deck itself said on the drone's console, and — after a few
   *  connections it accepted and sent nothing on — which chip is silent and
   *  what to do (agent camera/diagnose.py). */
  deck?: {
    lines: string[];
    silent_connects: number;
    diagnosis: { code: "gap8_silent" | "no_capture" | "send_stuck" | "unknown"; message: string } | null;
  };
};

export type CameraRate = {
  window_s: number;
  frames: number;
  fps: number;
  mean_bytes: number | null;
  /** Includes the gap still open, so a feed that just stopped shows it. */
  longest_gap_s: number | null;
  format: "raw" | "jpeg" | null;
};

export type DeckTiming = {
  lines: number;
  capture_ms: number;
  encoding_ms: number;
  transfer_ms: number;
  bytes: number;
  /** What the GAP8 could deliver if nothing downstream held it up. */
  deck_fps: number | null;
};

export type Intent = {
  up: boolean; down: boolean;
  forward: boolean; back: boolean;
  left: boolean; right: boolean;
  yaw_left: boolean; yaw_right: boolean;
};

export const EMPTY_INTENT: Intent = {
  up: false, down: false, forward: false, back: false,
  left: false, right: false, yaw_left: false, yaw_right: false,
};

export const HEARTBEAT_MS = 100;

let port = 8765;
let token = "";
/** Why the agent is not running, from the shell; null while it runs. */
let agentExit: string | null = null;

export async function connectToShell(): Promise<void> {
  port = await invoke<number>("agent_port");
  token = await invoke<string>("agent_token");
}

/**
 * Follow the shell's account of the agent stopping (lib.rs, `agent-exit`).
 *
 * Listens first, then asks once: an agent that died before this window
 * existed — the failure most in need of explaining — is only in the shell's
 * state, and asking before listening could miss one that dies in between.
 */
export async function watchAgentExit(onExit: (reason: string | null) => void): Promise<UnlistenFn> {
  const unlisten = await listen<string>("agent-exit", (event) => {
    agentExit = event.payload;
    onExit(agentExit);
  });
  agentExit = (await invoke<string | null>("agent_exit")) ?? agentExit;
  onExit(agentExit);
  return unlisten;
}

/** Where the agent writes its log on this computer. */
export const agentLogPath = () => invoke<string | null>("agent_log_path");

const base = () => `http://127.0.0.1:${port}`;

export class AgentError extends Error {}

const HEALTH_PROBE_MS = 2000;

/**
 * Is anything answering on the agent's port at all?
 *
 * `no-cors`, so the answer does not depend on CORS: any reply resolves (as an
 * opaque response) and only a refused connection rejects. That is the one
 * distinction a failed command needs — "the agent is not running" and "the
 * agent is running but this request did not get through" were both reported
 * as "Could not reach the flight agent. Restart DroneDeck."
 */
async function agentAnswers(): Promise<boolean> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), HEALTH_PROBE_MS);
  try {
    await fetch(`${base()}/health`, { mode: "no-cors", cache: "no-store", signal: controller.signal });
    return true;
  } catch {
    return false;
  } finally {
    window.clearTimeout(timer);
  }
}

/** What to tell the operator when a command's request never got an answer. */
async function unreachable(path: string): Promise<string> {
  if (await agentAnswers()) {
    return `The flight agent is running but did not answer ${path.split("?")[0]}. ` +
      "Try again — if it keeps failing, the agent's log says why.";
  }
  // When the shell knows why, the "Not connected" banner shows it; repeating
  // it here would put the same paragraph on screen twice.
  return agentExit
    ? "The flight agent on this computer is not running."
    : "The flight agent on this computer is not running. Quit and reopen DroneDeck.";
}

/**
 * The URL an <img> loads camera frames from.
 *
 * A bare URL rather than a fetch, because an <img> cannot send the control
 * token in a header — and the agent serves this route unauthenticated for that
 * reason: it is read-only, it commands nothing, and the agent binds localhost.
 * `stamp` defeats the cache so a live feed does not freeze on the first frame.
 */
export const cameraFrameUrl = (stamp: number) => `${base()}/camera/frame?t=${stamp}`;

async function command<T>(path: string, body?: unknown, method: "GET" | "POST" = "POST"): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${base()}${path}`, {
      method,
      headers: { "Content-Type": "application/json", "X-Agent-Token": token },
      body: method === "POST" ? JSON.stringify(body ?? {}) : undefined,
    });
  } catch {
    throw new AgentError(await unreachable(path));
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    // The agent's refusals are written for the operator; show them verbatim.
    const detail = payload?.detail;
    throw new AgentError(
      typeof detail === "string" ? detail : "That did not work. See the agent log.",
    );
  }
  return payload as T;
}

export const api = {
  signIn: (email: string, password: string) =>
    command<Session>("/auth/sign-in", { email, password }),
  signOut: () => command<Session>("/auth/sign-out"),
  setMode: (mode: Mode) => command<Session>("/session/mode", { mode }),
  /** Start a session: "flight" (Control — every check, it can fly) or
   *  "capture" (the Capture page — the radio and the camera deck only, it
   *  never flies). Callers that pass nothing start a flight session. */
  start: (purpose: "flight" | "capture" = "flight") => command<Session>("/session/start", { purpose }),
  confirmArea: (acceptUnassisted = false) =>
    command<Session>("/session/confirm", { accept_unassisted: acceptUnassisted }),
  healthTest: () => command<Session>("/session/health-test"),
  retry: () => command<Session>("/session/retry"),
  /** The hard switch: motors off, restart the drone over the radio, then every
   *  check again — this session, standing where a new one stands. */
  resetDrone: () => command<Session>("/session/reset-drone"),
  runProgram: (height_m: number, hold_s: number, ambient: string) =>
    command<Session>("/session/program", { height_m, hold_s, ambient }),
  armManual: (ambient: string) => command<Session>("/session/manual/arm", { ambient }),
  /** Rise to a height and hold it, in Manual, without holding W. Not the Auto
   *  hover test — that one needs base stations; this works on the barometer. */
  holdManual: (height_m: number) => command<Session>("/session/manual/hold", { height_m }),
  cameraStatus: () => command<CameraStatus>("/camera", undefined, "GET"),
  cameraWifi: () => command<CameraWifi>("/camera/wifi", undefined, "GET"),
  cameraRecording: () => command<Recording>("/camera/recording", undefined, "GET"),
  /** Restart the drone over the radio so its camera rejoins and reports its
   *  address. Between sessions only. */
  reconnectCamera: () => command<CameraWifi>("/camera/wifi/rejoin"),
  setupState: () => command<SetupState>("/setup", undefined, "GET"),
  setupCheck: () => command<SetupState>("/setup/check"),
  /** The terminal command that installs the drone's software on this machine
   *  (`dronedeck setup`). Set up shows it to copy: the install runs in a
   *  terminal, with DroneDeck quit, not from a button (setup.txt). */
  setupCommand: () => command<SetupCommand>("/setup/command", undefined, "GET"),
  recordedFrames: (after: number, limit: number) =>
    command<{ frames: RecordedFrameInfo[] }>(
      `/camera/recording/frames?after=${after}&limit=${limit}`, undefined, "GET"),
  /** Hold the drone on standby now (vitals, camera). Never arms. */
  connectDrone: () => command<Session>("/drone/connect"),
  /** Release the radio until Connect, for another tool. */
  disconnectDrone: () => command<Session>("/drone/disconnect"),
  land: () => command<Session>("/session/land"),
  emergencyStop: () => command<Session>("/session/emergency-stop"),
  endSession: () => command<Session>("/session/end"),
  syncNow: () => command<SyncStatus>("/sync/now"),

  // History: this laptop's own record of every session.
  listSessions: (limit = 20, mode?: Mode) =>
    command<SessionRecord[]>(
      `/history/sessions?limit=${limit}${mode ? `&mode=${mode}` : ""}`, undefined, "GET"),
  readSession: (id: string) =>
    command<SessionRecord>(`/history/sessions/${encodeURIComponent(id)}`, undefined, "GET"),
  planLimits: () => command<PlanLimits>("/plan/limits", undefined, "GET"),
  rooms: () => command<RoomView[]>("/rooms", undefined, "GET"),
  room: (id: string) => command<RoomView>(`/rooms/${encodeURIComponent(id)}`, undefined, "GET"),
  mission: (id: string) =>
    command<MissionView>(`/missions/${encodeURIComponent(id)}`, undefined, "GET"),
  saveRoom: (room: Room) => command<RoomView>("/rooms", room),
  deleteRoom: (id: string) => command<{ deleted: string }>(`/rooms/${encodeURIComponent(id)}/delete`),
  missions: () => command<MissionView[]>("/missions", undefined, "GET"),
  saveMission: (mission: Mission) => command<MissionView>("/missions", mission),
  deleteMission: (id: string) =>
    command<{ deleted: string }>(`/missions/${encodeURIComponent(id)}/delete`),
  /** Check a draft the agent has not saved — the agent's own validation. */
  validateMission: (mission: Mission, room: Room | null) =>
    command<MissionView>("/missions/validate", { mission, room }),
  /** The mission checked from where the drone is now (its position is the
   *  start) — what Start will be checked against. */
  missionFromDrone: (id: string) =>
    command<FlyingPlan>(`/missions/${encodeURIComponent(id)}/from-drone`, undefined, "GET"),
  /** The plan that will fly for an UNSAVED draft — nothing is saved. */
  fitDraft: (mission: Mission, room: Room) => command<FlyingPlan>("/missions/fit", { mission, room }),
  roomCoverage: (id: string) =>
    command<RoomCoverage>(`/rooms/${encodeURIComponent(id)}/coverage`, undefined, "GET"),
  startSurvey: (roomId: string) =>
    command<SurveyStatus>(`/rooms/${encodeURIComponent(roomId)}/survey/start`),
  surveyStatus: () => command<SurveyStatus>("/survey", undefined, "GET"),
  forgetCoverage: (roomId: string) =>
    command<RoomView>(`/rooms/${encodeURIComponent(roomId)}/coverage/forget`),
  stopSurvey: (save: boolean) => command<{ room: RoomView | null }>("/survey/stop", { save }),
  baseStations: () => command<{ stations: BaseStation[] }>("/basestations", undefined, "GET"),
  setBaseStationChannel: (port: string, channel: number) =>
    command<{ port: string; channel: number }>("/basestations/channel", { port, channel }),
  stationStatus: () => command<StationStatus>("/station", undefined, "GET"),
  /** Each measured station's place and facing, for the tripods on the map. */
  stationPoses: () => command<{ stations: StationMarker[] }>("/station/poses", undefined, "GET"),
  /** Manual's room, from the base stations alone (session.manual_space). */
  stationSpace: () => command<StationSpace>("/station/space", undefined, "GET"),
  /** Measure every station the drone receives, from where it sits. `cleared`:
   *  stations stored before and not seen this time — their place is gone. */
  measureStation: () => command<{ message: string; stations?: number[]; cleared?: number[] }>("/station/measure"),
  /** Every reason Start would refuse this mission now, with what to do —
   *  the same list Start refuses on (agent Session.mission_blockers). */
  missionBlockers: (missionId: string) =>
    command<{ blockers: MissionBlocker[] }>(`/missions/${encodeURIComponent(missionId)}/blockers`, undefined, "GET"),
  /** The DPP switch for this session. */
  setProcessing: (on: boolean) => command<Session>("/session/processing", { on }),
  /** The auto-capture switch. Applies at once, in either mode. */
  setCaptureAuto: (auto: boolean) => command<Session>("/session/capture", { auto }),
  /** Save the camera's newest frame into the session, by hand. */
  captureFrame: () => command<RecordedFrameInfo>("/camera/capture"),
  /** A recorded session's frames — "manual": its captures made by hand. */
  sessionFrames: (sessionId: string, source?: "auto" | "manual") =>
    command<{ frames: RecordedFrameInfo[] }>(
      `/history/sessions/${encodeURIComponent(sessionId)}/frames${source ? `?source=${source}` : ""}`,
      undefined, "GET"),
  /** The session's manual captures, oldest first. */
  manualCaptures: () =>
    command<{ frames: RecordedFrameInfo[] }>("/camera/recording/frames?source=manual&limit=2000", undefined, "GET"),
  /** Which way the arrow keys move the drone. Works in the air. */
  setKeyFrame: (frame: KeyFrameChoice) => command<Session>("/controls/frame", { frame }),
  /** "I'm here": the drone's position now becomes the operator's spot. */
  markOperatorHere: () => command<Session>("/controls/operator", { from_drone: true }),
  /** Forget the marked spot; "away" is measured from where it takes off. */
  clearOperator: () => command<Session>("/controls/operator", { clear: true }),
  flightResult: (flightId: string) =>
    command<FlightResult>(`/flights/${encodeURIComponent(flightId)}/result`, undefined, "GET"),
  playback: (flightId: string) =>
    command<FlightPlayback>(`/history/flights/${encodeURIComponent(flightId)}/playback`, undefined, "GET"),
  flights: (mode?: Mode) =>
    command<FlightList>(`/history/flights${mode ? `?mode=${mode}` : ""}`, undefined, "GET"),
  processFlight: (flightId: string) =>
    command<ProcessingJob>(`/flights/${encodeURIComponent(flightId)}/process`),
  runMission: (missionId: string, ambient: string) =>
    command<Session>("/session/mission", { mission_id: missionId, ambient }),
  readSamples: (id: string, variables: readonly string[], mode?: Mode) =>
    command<SampleRow[]>(
      `/history/sessions/${encodeURIComponent(id)}/samples?vars=${encodeURIComponent(variables.join(","))}${mode ? `&mode=${mode}` : ""}`,
      undefined, "GET",
    ),
};

export type FlightRecord = {
  id: string;
  mode: string;
  program: string | null;
  started_at: string;
  ended_at: string | null;
  outcome: string | null;
  abort_reason: string | null;
  /** The data pipeline for this flight: null when DPP was off as it began,
   *  "pending" until it lands, then queued / running / done / failed. */
  processing?: "pending" | ProcessingJob["state"] | null;
  processing_error?: string | null;
};

export type SessionRecord = {
  id: string;
  operator_id: string;
  operator_email: string;
  operator_name: string | null;
  drone_hardware_id: string | null;
  mode: string;
  /** Every mode the session used; a session can switch between flights. */
  modes: string[];
  assisted: boolean;
  started_at: string;
  ended_at: string | null;
  end_reason: string | null;
  duration_s: number | null;
  flights: FlightRecord[];
  summary: {
    samples: number;
    battery_start_v: number | null;
    battery_end_v: number | null;
    battery_min_v: number | null;
    max_height_m: number | null;
    temp_min_c: number | null;
    temp_max_c: number | null;
  };
};

export type SampleRow = { recorded_at: string } & Record<string, number | string | null>;

/** One recorded flight to watch again (GET /history/flights/{id}/playback,
 *  agent playback.py). Every time is seconds from the start of the recording —
 *  the mission report's clock. Nothing in it is simulated. */
export type PlaybackSample = {
  t_s: number;
  /** The room's frame — the frame the plan is drawn in. */
  x_m: number | null;
  y_m: number | null;
  /** Height above the floor where it took off. */
  z_m: number | null;
  /** A base station was received: the position was measured. False is the
   *  drone flying on its own estimate; null in a file older than the column. */
  positioned: boolean | null;
  yaw_deg: number | null;
  roll_deg: number | null;
  pitch_deg: number | null;
  /** Horizontal speed. */
  speed_m_s: number | null;
  vz_m_s: number | null;
  battery_v: number | null;
  /** Corrected temperature, in the flight's temp_unit. */
  temp: number | null;
  pressure_hpa: number | null;
  thrust: number | null;
  point_id: string | null;
};

export type PlaybackControl = {
  t_s: number;
  /** The manual flight system's state: armed, flying, landing, landed, stopped, idle. */
  state: string;
  /** Keys the operator held, as recorded; "" when none. */
  keys: string;
  /** Where the loop was sending the drone: x, y and height above the floor. */
  target: [number, number, number] | null;
  drift_m: number | null;
  height_error_m: number | null;
};

export type PlaybackEvent = {
  t_s: number;
  kind: "state" | "keys" | "hold" | "position" | "gap";
  text: string;
  point_id: string | null;
};

export type FlightReportLeg = {
  to: string;
  label: string | null;
  leg_m: number;
  planned_s: number;
  transit_s: number | null;
  reached: boolean;
  settle_s: number | null;
  hold_s: number | null;
  hold_required_s: number | null;
  hold_long_enough: boolean | null;
  hold_drift_median_m: number | null;
  hold_drift_worst_m: number | null;
  notes: string[];
};

/** Where a flight is played from: this laptop's files, or Supabase. */
export type FlightSource = "this_computer" | "supabase";

/** One flight that can be played back (GET /history/flights): this laptop's
 *  and everyone's in Supabase, each once (agent playback.list_flights). */
export type FlightListItem = {
  id: string;
  session_id: string | null;
  started_at: string | null;
  ended_at: string | null;
  mode: string | null;
  program: string | null;
  outcome: string | null;
  abort_reason: string | null;
  operator_email: string | null;
  operator_name: string | null;
  /** Its plan as flown is here or in Supabase: the map can draw the mission. */
  has_plan: boolean;
  on_this_computer: boolean;
  in_supabase: boolean;
  source: FlightSource;
  /** This laptop's upload of it: null for a flight flown elsewhere. */
  upload: "uploading" | "uploaded" | null;
};

export type FlightList = {
  flights: FlightListItem[];
  /** Whether Supabase was asked: signed out or unreachable, only this
   *  laptop's flights are listed, and this says why. */
  cloud: "ok" | "signed_out" | "error";
  cloud_error: string | null;
};

export type FlightPlayback = {
  flight_id: string;
  source: FlightSource;
  started_at: string | null;
  duration_s: number;
  temp_unit: string | null;
  truncated: boolean;
  /** Each a sentence for the operator: what is missing, and why. */
  notes: string[];
  /** The room's map, or the default area for a flight that kept no room. */
  outer: OuterBound;
  room: RoomView | null;
  /** The plan AS FLOWN — a mission flight from the app only. */
  mission: MissionView | null;
  moves: { point_id: string; distance_m: number }[];
  report: { legs: FlightReportLeg[]; estimate_s: number; flown_s: number | null; notes: string[]; longer_than_estimate: boolean } | null;
  samples: PlaybackSample[];
  control: PlaybackControl[];
  gaps: { from_s: number; to_s: number }[];
  events: PlaybackEvent[];
};

/** Set up: installing the drone's software (drone_setup.py). */
export type SetupPart = "main" | "lighthouse" | "camera" | "wifi";
/** GET /setup/command — the exact line for this machine, ready to paste. */
export type SetupCommand = { command: string; check: string };

export type SetupState = {
  phase: "idle" | "checking" | "ready" | "installing" | "unplug" | "verifying" | "done" | "failed";
  message: string | null;
  facts: {
    battery_v: number | null;
    ai_deck: boolean | null;
    /** The camera software is on record, but not for the AI deck fitted now
     *  — a replaced deck, or a drone from before decks were recorded. */
    deck_unrecorded?: boolean;
  } | null;
  parts: Partial<Record<SetupPart, "installed" | "needed" | "installing" | "done" | "absent">>;
  current: SetupPart | null;
  progress: number;
  labels: Record<SetupPart, string>;
  order: SetupPart[];
};

/** A session's camera recording (camera/recording.py). */
export type Recording =
  | { recording: false }
  | {
      recording: true;
      session_id: string;
      count: number;
      latest_seq: number;
      latest_t_s: number | null;
      upload_fps: number;
      write_errors: number;
      /** Frames are recorded automatically (auto-capture on). False: only
       *  manual captures are saved. Absent from older agents: true. */
      auto?: boolean;
    };

export type RecordedFrameInfo = {
  seq: number; t_s: number; recorded_at: string; upload: boolean;
  /** "auto", or "manual" — captured by hand. Absent from older agents. */
  source?: "auto" | "manual";
  /** The inspection point being held when it was taken (history only). */
  point_id?: string | null;
};

/**
 * One recorded frame read back from disk, by number or "latest". A fetch with
 * the token, not an <img> URL: recorded frames are session data. Null on 204.
 */
export async function recordedFrame(which: number | "latest"): Promise<Blob | null> {
  const response = await fetch(`${base()}/camera/recording/frame/${which}`, {
    headers: { "X-Agent-Token": token },
    cache: "no-store",
  });
  if (response.status === 204) return null;
  if (!response.ok) throw new AgentError("That frame could not be read.");
  return response.blob();
}

/** One frame of a session that has ended, read from this laptop's disk — a
 *  fetch with the token, like recordedFrame. Null when it is not there. */
export async function sessionFrame(sessionId: string, seq: number): Promise<Blob | null> {
  const response = await fetch(
    `${base()}/history/sessions/${encodeURIComponent(sessionId)}/frames/${seq}`, {
      headers: { "X-Agent-Token": token },
      cache: "no-store",
    });
  if (response.status === 404) return null;
  if (!response.ok) throw new AgentError("That frame could not be read.");
  return response.blob();
}

/** A frame's upscaled copy (the display chain, Real-ESRGAN then CLAHE, made
 *  after the session's processing) — for looking at, never as evidence.
 *  "pending": not made yet; "failed": never will be at this version. */
export type UpscaledFrame =
  | { kind: "ready"; blob: Blob }
  | { kind: "pending"; message: string }
  | { kind: "failed"; message: string };

export async function sessionFrameUpscaled(sessionId: string, seq: number): Promise<UpscaledFrame> {
  const response = await fetch(
    `${base()}/history/sessions/${encodeURIComponent(sessionId)}/frames/${seq}/upscaled`, {
      headers: { "X-Agent-Token": token },
      cache: "no-store",
    });
  if (response.ok) return { kind: "ready", blob: await response.blob() };
  if (response.status === 404) {
    const body = (await response.json().catch(() => null)) as
      { detail?: { state?: string; message?: string } | string } | null;
    const detail = body?.detail;
    if (typeof detail === "object" && (detail.state === "pending" || detail.state === "failed")) {
      return { kind: detail.state, message: detail.message ?? "" };
    }
    // An agent from before the display chain answers a plain 404.
    return { kind: "failed", message: "There is no upscaled copy of this frame on this computer." };
  }
  throw new AgentError("The upscaled copy could not be read.");
}

/** Where the AI deck is in joining the operator's Wi-Fi. No password, ever. */
/** What the deck's radio is set to. `brownout`: held at reduced power
 *  until the battery is unplugged (camera/wifi.py DeckLink). */
export type DeckLink = {
  bandwidth_mhz: number;
  power_dbm: number | null;
  beacon_timeout_s: number;
  brownout: boolean;
};

export type CameraWifi = {
  ssid: string | null;
  phase: "not-set" | "waiting" | "sending" | "joining" | "joined" | "reconnecting" | "failed";
  /** The deck's address on that network, once it has one. */
  ip: string | null;
  message: string | null;
  /** The deck's last reported signal, dBm. Below about -75 it drops out. */
  rssi?: number | null;
  /** Times it has dropped off since it last joined. */
  drops?: number;
  /** The address is remembered from an earlier join — unverified. */
  remembered?: boolean;
  /** Only a restart of the drone gets it onto this network now. */
  needs_restart?: boolean;
  /** Why the deck last dropped off the network, in words, as it said
   *  (wifi-recovery.patch); null when it has not said. */
  reason?: string | null;
  /** The deck radio's settings, read back by its ESP32 (wifi-link.patch);
   *  null from a deck without it. */
  link?: DeckLink | null;
};

// ── the live socket ───────────────────────────────────────────────────

export type LiveHandlers = {
  onSession: (session: Session) => void;
  /** The agent answered but refused this window — see `onError` callers. */
  onRefused: (message: string) => void;
  onSync: (status: SyncStatus) => void;
  onTelemetry: (telemetry: Telemetry) => void;
  onConnection: (connected: boolean) => void;
  onCameraWifi?: (state: CameraWifi) => void;
  onSetup?: (state: SetupState) => void;
};

export class LiveConnection {
  private socket: WebSocket | null = null;
  private heartbeat: number | null = null;
  private retry: number | null = null;
  private closed = false;

  constructor(private handlers: LiveHandlers) {}

  open(): void {
    this.closed = false;
    const socket = new WebSocket(`ws://127.0.0.1:${port}/ws/live`);
    this.socket = socket;

    socket.addEventListener("open", () => {
      // The token goes in the first message, not the URL: a query string ends
      // up in the access log, and this one grants control of a drone.
      socket.send(JSON.stringify({ type: "auth", token }));
      this.handlers.onConnection(true);
      this.heartbeat = window.setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "heartbeat" }));
        }
      }, HEARTBEAT_MS);
    });

    socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.type === "error") {
        // The agent is up but will not talk to this window: almost always
        // another copy of the app already holds the port, so this window is
        // talking to that copy's agent with a token it does not know.
        this.closed = true;
        this.handlers.onRefused(
          "Another copy of DroneDeck is already running on this computer. " +
          "Quit it, then reopen this window.",
        );
        socket.close();
        return;
      }
      if (message.type === "session") this.handlers.onSession(message as Session);
      else if (message.type === "sync") this.handlers.onSync(message as SyncStatus);
      else if (message.type === "telemetry") this.handlers.onTelemetry(message as Telemetry);
      else if (message.type === "camera_wifi") this.handlers.onCameraWifi?.(message as CameraWifi);
      else if (message.type === "setup") this.handlers.onSetup?.(message as SetupState);
    });

    const dropped = () => {
      this.stopHeartbeat();
      this.handlers.onConnection(false);
      // The agent is a child of this app, so a dropped socket means it is
      // restarting rather than gone. Keep trying quietly.
      if (!this.closed && this.retry === null) {
        this.retry = window.setTimeout(() => {
          this.retry = null;
          this.open();
        }, 1000);
      }
    };
    socket.addEventListener("close", dropped);
    socket.addEventListener("error", dropped);
  }

  sendIntent(intent: Intent): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: "intent", keys: intent }));
    }
  }

  close(): void {
    this.closed = true;
    this.stopHeartbeat();
    this.socket?.close();
  }

  private stopHeartbeat(): void {
    if (this.heartbeat !== null) window.clearInterval(this.heartbeat);
    this.heartbeat = null;
  }
}

/**
 * Every binding, shown on screen.
 *
 * `field` is the intent field a key drives, so a held key can light up. Land
 * and Emergency stop have no intent field: Land is a command, and the stop is
 * hold-to-activate with no keyboard shortcut at all.
 */
export const KEY_LABELS: { keys: { label: string; field?: keyof Intent }[]; action: string }[] = [
  { keys: [{ label: "↑", field: "forward" }, { label: "↓", field: "back" }], action: "Away from you / back (Room: the room's forward / back)" },
  { keys: [{ label: "←", field: "left" }, { label: "→", field: "right" }], action: "Round to your left / right (Room: the room's left / right)" },
  { keys: [{ label: "W", field: "up" }, { label: "S", field: "down" }], action: "Rise / descend, gently" },
  { keys: [{ label: "A", field: "yaw_left" }, { label: "D", field: "yaw_right" }], action: "Rotate left / right" },
  { keys: [{ label: "L" }], action: "Land gracefully" },
];
