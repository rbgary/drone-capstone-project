/**
 * Layout harness — renders the real pages with worst-case data, in a browser.
 *
 * The app itself cannot be measured in a browser: it reaches the agent through
 * Tauri's `invoke`, which does not exist outside the window. This mounts the
 * same components with the longest values they will ever be handed (a full
 * email address, a hardware id, a failed check with a long reason) so overflow
 * can be measured at the window's minimum width instead of guessed at.
 *
 * Not shipped: `vite build` only builds index.html.
 */

import { createRoot } from "react-dom/client";
import { HomePage } from "@/pages/home/HomePage";
import { ControlPage } from "@/pages/control/ControlPage";
import { ModeLock } from "@/pages/control/ModeLock";
import type { FlowStart } from "@/pages/control/auto/MissionFlow";
import type { Run } from "@/App";
import { SensorWindow, WINDOWS } from "@/pages/windows/SensorWindow";
import { WindowLog } from "@/pages/sessions/WindowLog";
import { SessionsPage } from "@/pages/sessions/SessionsPage";
import { FlightPlaybackView } from "@/pages/sessions/Playback";
import { PlaybackPage } from "@/pages/playback/PlaybackPage";
import { CapturePage } from "@/pages/capture/CapturePage";
import { StartupPage } from "@/pages/startup/StartupPage";
import { Install, SetupPage } from "@/pages/setup/SetupPage";
import { WifiPage } from "@/pages/wifi/WifiPage";
import {
  EMPTY_INTENT, connectToShell,
  type CameraWifi, type FlightList, type FlightPlayback, type MissionView, type PlanLimits, type RoomView, type Session,
  type SetupState,
  type Telemetry,
} from "@/lib/agent";
import type { LogLine } from "@/lib/commandLog";
import { FlightStrip } from "@/components/FlightStrip";
import { Sidebar } from "@/components/Sidebar";
import type { History } from "@/App";
import { reportInto } from "@/measure";
import { MissionStep } from "@/pages/control/auto/plan/MissionStep";
import "./styles.css";

const session: Session = {
  state: "checks_failed",
  mode: "manual",
  operator: { id: "a0afd4e8", email: "zebachsmoak@gmail.com", name: null },
  drone: { hardware_id: "cf-4a3b2c1d0e9f8a7b", battery_v: 3.82, endurance_s: 420 },
  session_id: "3f1c",
  activity: null,
  checks: [
    { key: "radio", label: "Radio", status: "passed", detail: "cf-4a3b2c1d0e9f8a7b answered", data: {} },
    { key: "battery", label: "Battery", status: "passed", detail: "3.82 V — the drone reports it can fly", data: {} },
    {
      key: "positioning", label: "Positioning", status: "failed",
      data: {},
      detail: "Only 0 of 4 base stations are being received. Stored geometry is not signal — re-run geometry estimation in cfclient before flying.",
    },
  ],
  health_test: {
    ok: false,
    motors: { passed: [1, 4], failed: [2, 3], ok: false },
    battery: { sag_v: 0.61, passed: false, idle_vbat: 4.12, pwm_ratio: 0 },
    battery_error: null,
  },
  retry_required: false,
  radio: { state: "connected", hardware_id: "cf-002f002a3334471239333335", message: null },
  mission: null,
  processing: {
    on: true, chosen: false, last_flight_id: "f1c2d3e4-flight",
    jobs: [{ flight_id: "f1c2d3e4-flight", state: "done", queued_at: "2026-09-29T10:00:00+00:00",
             finished_at: "2026-09-29T10:00:02+00:00", error: null }],
  },
  controls: { key_frame: "operator", operator: [0.4, -1.8], operator_marked_at: "2026-09-30T09:00:00+00:00", live: null },
  flight: null,
  message: "The checks did not pass, so the drone will not arm.",
  can_fly: false,
  restoring: false,
  assisted: false,
  unassisted_reason: "No base station signal is reaching the drone where it is now. Check the station is on (front LED solid green) and can see the top of the drone from where it sits.",
};

const values: Record<string, number> = {
  "stateEstimate.x": 0.12, "stateEstimate.y": -0.34, "stateEstimate.z": 0.31,
  "stateEstimate.vx": 0.02, "stateEstimate.vy": -0.01, "stateEstimate.vz": 0.0,
  "stabilizer.roll": 1.2, "stabilizer.pitch": -0.8, "stabilizer.yaw": 178.4,
  "stabilizer.thrust": 38412, "supervisor.info": 0,
  "pm.vbat": 3.82, "pm.state": 0, "sys.canfly": 1,
  "motor.m1": 38000, "motor.m2": 38210, "motor.m3": 37980, "motor.m4": 38110,
  "baro.temp": 28.6, "baro.pressure": 1013.2,
  "acc.x": 0.01, "acc.y": -0.02, "acc.z": 1.0,
  "gyro.x": 0.4, "gyro.y": -0.3, "gyro.z": 0.1,
  "lighthouse.bsReceive": 0b1111, "lighthouse.bsActive": 0b1111,
  "lighthouse.bsCalVal": 0b0011, "lighthouse.bsGeoVal": 0b0001,
  "lighthouse.bsAvailable": 0b1111,
  "kalman.varPX": 0.004, "kalman.varPY": 0.003, "kalman.varPZ": 0.001,
};
const telemetry: Telemetry = { values, height_m: 0.31, positioned: true, at: 0 };
const history: History = Array.from({ length: 120 }, (_, i) => ({
  t: i / 10, values, height_m: 0.3 + Math.sin(i / 8) * 0.02,
}));

const noop = async () => {};
/**
 * The Control page's actions really run, against the fake fetch below — so
 * Save in the mission editor saves and goes back to the list. Anything the
 * fake does not answer fails quietly, as a refused command would.
 */
const runHere: Run = async (action, label) => {
  try { await action(); } catch (e) { console.warn(`harness: ${label ?? "action"} failed`, e); }
};

/**
 * `?agent=<token>` measures the session views against the agent already running
 * on this machine, by standing in for the Tauri bridge the browser lacks. The
 * token is per-launch and never stored here; without the parameter the harness
 * renders the static pages exactly as before.
 */
const query = new URLSearchParams(location.search);
const agentToken = query.get("agent");

// `?theme=dark|light` pins the tokens, so both themes get measured rather than
// whichever one this machine happens to be set to.
const theme = query.get("theme");
// `?map=2d|3d` pins the room map's view (RoomMap remembers it per machine).
const mapView = query.get("map");
if (mapView === "2d" || mapView === "3d") {
  try { localStorage.setItem("dronedeck.auto.mapView", mapView); } catch { /* private window */ }
}
if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
if (agentToken) {
  (window as unknown as { __TAURI_INTERNALS__: unknown }).__TAURI_INTERNALS__ = {
    invoke: async (command: string) =>
      command === "agent_port" ? Number(query.get("port") ?? 8765)
        : command === "agent_token" ? agentToken
          : null,
  };
  await connectToShell();
}

/**
 * The Auto page's plans, served in place of the agent when no agent is given.
 *
 * Worst case on purpose: a long room and mission name, every obstacle kind, a
 * label that wraps, and one leg the agent flags — so the editor, the list and
 * the problem panel are measured with the most they will ever show. Only the
 * plan routes are answered here; everything else goes to the real fetch.
 */
const sampleLimits: PlanLimits = {
  outer: { vertices: [[-2, -2], [2, -2], [2, 2], [-2, 2]], measured: false },
  default_clearance_m: 0.25, min_hold_s: 2, z_min_m: 0.1, z_max_m: 1.0,
  move_speed_m_s: 0.2, climb_rate_m_s: 0.15,
};
const sampleRoom: RoomView = {
  format: 1, id: "lab", name: "Engineering lab, north bay (bench row B and the pump skid)",
  geofence: { shape: "rectangle", vertices: [[-1.7, -1.5], [1.7, -1.5], [1.7, 1.5], [-1.7, 1.5]], z_min: 0.1, z_max: 1.0 },
  obstacles: [
    { id: "bench", kind: "rectangle", label: "Bench", points: [[-0.4, -0.3], [0.4, 0.3]], height_m: 0.9 },
    { id: "pillar", kind: "circle", label: "Pillar", points: [[1.1, -0.9]], radius: 0.2, height_m: null },
    { id: "rail", kind: "line", label: "Guard rail", points: [[-1.4, 1.1], [-0.4, 1.1]], height_m: 1.1 },
  ],
  coverage: null, clearance_m: 0.25, revision: 3,
  created_at: "2026-09-28T10:00:00+00:00", updated_at: "2026-09-28T11:30:00+00:00",
  outer: sampleLimits.outer,
  problems: [{ code: "coverage_not_measured", severity: "warning", where: null,
    message: "This room's Lighthouse coverage has not been measured, so the fence is checked against the agent's default flying area instead. Measure it before trusting the edges of the room." }],
  valid: true,
};
const sampleMission: MissionView = {
  format: 1, id: "m-long", name: "Weekly thermal sweep of pumps, valves and the compressor manifold",
  room_id: "lab", home: [-1.2, -1.0],
  points: [
    { id: "P1", label: "Pump 1 bearing housing", x_m: -1.2, y_m: 0.6, z_m: 0.4, hold_s: 6 },
    { id: "P2", label: "Pump 2", x_m: 0.9, y_m: 0.7, z_m: 0.5, hold_s: 5 },
    { id: "P3", label: "Compressor manifold, upper flange", x_m: 1.2, y_m: 0.0, z_m: 0.6, hold_s: 8 },
    { id: "P4", label: null, x_m: 0.0, y_m: -1.0, z_m: 0.4, hold_s: 5 },
  ],
  cruise_height_m: 0.4, return_to_start: false, end_point_id: "P3", revision: 2, flown_revision: 1,
  created_at: "2026-09-27T09:00:00+00:00", updated_at: "2026-09-28T11:31:00+00:00",
  problems: [
    sampleRoom.problems[0],
    { code: "leg_near_obstacle", severity: "error", where: "P2 → P3", message: "The leg P2 → P3 passes 0.12 m from obstacle Pillar; it needs 0.25 m." },
  ],
  valid: false, path_length_m: 8.4, estimated_duration_s: 96,
};
/**
 * A recorded flight for Playback, worst case: the long mission above flown to
 * its end point at 10 Hz, with a stretch flown on the drone's own estimate (no
 * base station), 1.6 s of samples lost on the link, the operator's keys, a
 * short hold — every kind of event, and more of them than fit a column.
 */
function samplePlaybackOf(mission: MissionView): FlightPlayback {
  const stops: [number, number, number, number][] = [
    [mission.home[0], mission.home[1], mission.cruise_height_m, 0],
    ...mission.points.slice(0, 3).map((p) => [p.x_m, p.y_m, p.z_m, p.hold_s] as [number, number, number, number]),
  ];
  const samples: FlightPlayback["samples"] = [];
  const control: FlightPlayback["control"] = [];
  const holds: [string, number, number][] = [];
  let t = 0;
  const at = (x: number, y: number, z: number, point: string | null, keys = "") => {
    const lost = t > 16 && t < 19.4;
    const gone = t > 30 && t < 31.6;
    if (!gone) {
      samples.push({ t_s: +t.toFixed(1), x_m: x + Math.sin(t) * 0.01, y_m: y, z_m: z, positioned: !lost,
        yaw_deg: -5 + Math.sin(t / 3) * 2, roll_deg: 1.2, pitch_deg: -0.6, speed_m_s: 0.18, vz_m_s: 0.01,
        battery_v: 4.05 - t / 200, temp: 22.4 + t / 100, pressure_hpa: 1013.2, thrust: 54000, point_id: point });
    }
    control.push({ t_s: +t.toFixed(1), state: t < 0.6 ? "armed" : "flying", keys, target: [x, y, z],
      drift_m: 0.03, height_error_m: 0.012 });
    t += 0.1;
  };
  for (let i = 0; i < 8; i++) at(stops[0][0], stops[0][1], i / 8 * stops[0][2], null);
  stops.slice(1).forEach(([x, y, z, hold], n) => {
    const [px, py, pz] = stops[n];
    const steps = Math.ceil(Math.hypot(x - px, y - py) / 0.02);
    for (let i = 1; i <= steps; i++) at(px + (x - px) * i / steps, py + (y - py) * i / steps, pz + (z - pz) * i / steps, null);
    const id = mission.points[n].id;
    const began = t;
    const held = n === 1 ? hold * 0.6 : hold;
    for (let i = 0; i < held * 10; i++) at(x, y, z, id, n === 2 && i > 30 && i < 40 ? "ArrowUp" : "");
    holds.push([id, began, t]);
  });
  const end = +t.toFixed(1);
  for (let i = 0; i < 20; i++) control.push({ ...control[control.length - 1], t_s: +(end + i / 10).toFixed(1), state: i < 15 ? "landing" : "landed" });
  const events: FlightPlayback["events"] = ([
    { t_s: 0, kind: "state", text: "Armed", point_id: null },
    { t_s: 0.6, kind: "state", text: "Took off", point_id: null },
    { t_s: 16.1, kind: "position", text: "Base stations lost for 3.4 s — the position was not measured, the drone flew on its own estimate", point_id: null },
    ...holds.flatMap(([id, a, b]) => [
      { t_s: +a.toFixed(1), kind: "hold" as const, text: `Holding at ${id}`, point_id: id },
      { t_s: +b.toFixed(1), kind: "hold" as const, text: `${id} hold ended`, point_id: id },
    ]),
    { t_s: 30.0, kind: "gap", text: "No samples for 1.6 s — data lost on the link", point_id: null },
    { t_s: end, kind: "state", text: "Landing", point_id: null },
    { t_s: end + 1.5, kind: "state", text: "Landed", point_id: null },
  ] as FlightPlayback["events"]).sort((a, b) => a.t_s - b.t_s);
  const keyAt = holds[2][1] + 3.1;
  events.push({ t_s: +keyAt.toFixed(1), kind: "keys", text: "Operator held ArrowUp", point_id: null },
              { t_s: +(keyAt + 0.9).toFixed(1), kind: "keys", text: "Keys released", point_id: null });
  events.sort((a, b) => a.t_s - b.t_s);
  return {
    flight_id: "f1c2d3e4-playback", source: "supabase", started_at: "2026-10-09T18:31:24+00:00", duration_s: +(end + 1.9).toFixed(1),
    temp_unit: "C", truncated: false,
    notes: ["The plan this flight flew could not be read: an example of a note long enough to wrap across the column at the narrowest width the window allows."],
    outer: sampleLimits.outer, room: sampleRoom, mission, moves: [],
    report: {
      estimate_s: 96, flown_s: end + 1.9, longer_than_estimate: false, notes: ["The trace has a 1.6 s gap at 30.0 s: samples were dropped."],
      legs: mission.points.slice(0, 3).map((p, n) => ({
        to: p.id, label: p.label, leg_m: 1.6, planned_s: 8.0, transit_s: 8.3, reached: true, settle_s: 0.4,
        hold_s: n === 1 ? p.hold_s * 0.6 : p.hold_s, hold_required_s: p.hold_s, hold_long_enough: n !== 1,
        hold_drift_median_m: 0.021, hold_drift_worst_m: 0.064,
        notes: n === 1 ? [`Held ${(p.hold_s * 0.6).toFixed(1)} s of the ${p.hold_s.toFixed(1)} s asked.`] : [],
      })),
    },
    samples, control,
    gaps: [{ from_s: 29.9, to_s: 31.6 }], events,
  };
}
const samplePlayback = samplePlaybackOf(sampleMission);
/** The flight list, worst case: a long name and program, every place a flight
 *  can be (here and uploading, here and uploaded, only in Supabase), both
 *  modes, an abort, and Supabase's error words beside it. */
const sampleFlights: FlightList = {
  cloud: query.get("cloud") === "error" ? "error" : query.get("cloud") === "out" ? "signed_out" : "ok",
  cloud_error: "could not list the flights: ConnectError — the network is unreachable from this laptop right now",
  flights: [
    { id: "f1", session_id: "s1", started_at: "2026-10-11T14:31:24+00:00", ended_at: "2026-10-11T14:32:06+00:00",
      mode: "auto", program: "mission:m-long@r12 — Weekly thermal sweep of pumps, valves and the compressor manifold",
      outcome: "done", abort_reason: null, operator_email: "zebachsmoak@gmail.com", operator_name: null,
      has_plan: true, on_this_computer: true, in_supabase: false, source: "this_computer", upload: "uploading" },
    { id: "f2", session_id: "s2", started_at: "2026-10-10T10:02:00+00:00", ended_at: "2026-10-10T10:02:40+00:00",
      mode: "auto", program: "mission:m1@r3", outcome: "aborted", abort_reason: "lighthouse_lost_in_flight",
      operator_email: "hannah.alexander.long.address@university.example.edu", operator_name: "Hannah Dorothea Alexander-Whitfield",
      has_plan: true, on_this_computer: false, in_supabase: true, source: "supabase", upload: null },
    { id: "f3", session_id: "s3", started_at: "2026-10-09T09:00:00+00:00", ended_at: null,
      mode: "manual", program: null, outcome: null, abort_reason: null,
      operator_email: "kevin@example.com", operator_name: "Kevin",
      has_plan: false, on_this_computer: true, in_supabase: true, source: "this_computer", upload: "uploaded" },
  ],
};
const sessionShown = session;
const sampleMissions: MissionView[] = [
  sampleMission,
  { ...sampleMission, id: "m-ok", name: "Pump check", points: sampleMission.points.slice(0, 2),
    problems: [sampleRoom.problems[0]], valid: true, flown_revision: null, estimated_duration_s: 41 },
];
/** A stand-in camera frame, 324x244 like the deck's, drawn once. */
let frameBlob: Promise<Blob> | null = null;
function sampleFrame(): Promise<Blob> {
  frameBlob ??= new Promise<Blob>((resolve) => {
    const canvas = document.createElement("canvas");
    canvas.width = 324;
    canvas.height = 244;
    const ctx = canvas.getContext("2d")!;
    const g = ctx.createLinearGradient(0, 0, 324, 244);
    g.addColorStop(0, "#202020");
    g.addColorStop(1, "#7a7a7a");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 324, 244);
    canvas.toBlob((b) => resolve(b ?? new Blob()), "image/png");
  });
  return frameBlob;
}

if (!agentToken) {
  const realFetch = window.fetch.bind(window);
  const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  // Saving is kept for the page's life, so Save → the list can be checked:
  // the saved mission comes back with its revision bumped, as the agent does.
  const rooms: RoomView[] = [sampleRoom];
  const missions: MissionView[] = [...sampleMissions];
  const upsert = <T extends { id: string }>(list: T[], item: T) => {
    const at = list.findIndex((x) => x.id === item.id);
    if (at < 0) list.push(item); else list[at] = item;
    return item;
  };
  window.fetch = async (input, init) => {
    const path = new URL(String(input), location.href).pathname;
    const post = init?.method === "POST";
    const body = post && typeof init?.body === "string" ? JSON.parse(init.body) : null;
    if (path === "/plan/limits") return json(sampleLimits);
    if (path === "/rooms" && body) return json(upsert(rooms, { ...sampleRoom, ...body }));
    if (path === "/rooms") return json(rooms);
    if (path.startsWith("/rooms/")) return json(rooms.find((r) => path.endsWith(r.id)) ?? sampleRoom);
    if (path === "/missions" && body) {
      const before = missions.find((m) => m.id === body.id);
      return json(upsert(missions, { ...sampleMission, problems: [], valid: true, ...body,
        revision: (before?.revision ?? 0) + 1, flown_revision: before?.flown_revision ?? null,
        updated_at: new Date().toISOString() }));
    }
    if (path === "/missions") return json(missions);
    if (path === "/missions/validate") return json(sampleMission);
    if (path.endsWith("/from-drone")) {
      return json({ position: [values["stateEstimate.x"], values["stateEstimate.y"]],
                    mission: { ...sampleMission, home: [values["stateEstimate.x"], values["stateEstimate.y"]] } });
    }
    if (path.startsWith("/flights/") && path.endsWith("/result")) {
      return json({
        flight_id: "f1c2d3e4-flight", job: sessionShown.processing.jobs[0],
        result: {
          flight_id: "f1c2d3e4-flight", pipeline_version: "0.1.0", stages: { clean: "stub", classify: "stub" },
          created_at: "2026-09-29T10:00:02+00:00", failures: [],
          points: ["P1", "P2", "P3"].map((id) => ({ point_id: id, verdict: "insufficient_data",
            reasons: ["No model gave a verdict: no classifier yet."], alerts: [] })),
          summary: { P1: { readings: 60, frames: 22 }, P2: { readings: 52, frames: 18 }, P3: { readings: 81, frames: 30 } },
        },
      });
    }
    // The camera, for the Capture page and the Camera tab: a live deck with
    // the measured rate, a frame drawn here, and five manual captures.
    if (path === "/camera") {
      return json({ live: true, kind: "ai-deck", reason: null, deck_fitted: true, width: 324, height: 244,
        connecting: false,
        rate: { window_s: 10, frames: 37, fps: 3.7, mean_bytes: 79056, longest_gap_s: 1.4, format: "raw" },
        deck_timing: { lines: 20, capture_ms: 76, encoding_ms: 0, transfer_ms: 182, bytes: 79056, deck_fps: 3.88 } });
    }
    if (path === "/camera/wifi") return json({ ssid: "VT Open WiFi", phase: "joined", ip: "100.96.55.59", message: null, rssi: -61 });
    if (path === "/camera/frame" || path.startsWith("/camera/recording/frame/")) return new Response(await sampleFrame());
    if (path === "/camera/recording") {
      return json({ recording: true, session_id: "3f1c", count: 412, latest_seq: 412, latest_t_s: 754.2,
        upload_fps: 2, write_errors: 0, auto: true, captured: 96, manual: 5 });
    }
    if (path === "/camera/recording/frames") {
      return json({ frames: [12, 88, 140, 301, 412].map((seq) => ({ seq, t_s: seq * 1.83,
        recorded_at: "2026-10-24T10:00:00+00:00", upload: true, source: "manual" })) });
    }
    if (path === "/camera/capture" && post) {
      return json({ seq: 413, t_s: 755.1, recorded_at: "2026-10-24T10:12:35+00:00", upload: true, source: "manual" });
    }
    // Two base stations at the room's corners, as /station/poses gives them:
    // the tripods on the map (stations.ts), facing back into the room.
    if (path === "/station/poses") {
      return json({ stations: [
        { id: 0, channel: 1, origin: [-2.3, 1.9, 1.62], forward: [0.721, -0.515, -0.464],
          left: [0.581, 0.814, 0], up: [0.377, -0.269, 0.886] },
        { id: 1, channel: 2, origin: [2.4, 2.1, 1.71], forward: [-0.78, -0.468, -0.416],
          left: [0.514, -0.857, 0], up: [-0.357, -0.214, 0.909] },
      ] });
    }
    // The station chain as the lab read it at 07:16 on 2026-10-24: both
    // stations measured and calibrated, and no light on the deck at all.
    if (path === "/station") {
      return json({ connected: true, measuring: false, light_sensors: 0, calibrated: [0, 1], active: [],
        received: [], measured: [0, 1], usable: [], uncertainty_cm: 501.7, ready: false,
        stations: [{ id: 0, channel: 1, calibrated: true, received: false, measured: true, active: false },
                   { id: 1, channel: 2, calibrated: true, received: false, measured: true, active: false }],
        deck: { detected: true, needs_fw: false },
        blocker: { stage: "light", text: "No light reaches the positioning deck (0 of 4 sensors).",
                   fix: "Check both base stations' front LEDs are solid green and can see the top of the drone. Still 0 with the drone 1 m in front of a station: switch the drone off and on; then reseat the positioning deck." } });
    }
    if (path === "/setup/command") {
      return json({ command: "/Applications/DroneDeck.app/Contents/MacOS/dronedeck-agent setup",
                    check: "/Applications/DroneDeck.app/Contents/MacOS/dronedeck-agent setup --check" });
    }
    // Manual's room: where the two stations above reach (agent /station/space).
    if (path === "/station/space") {
      const reach: [number, number][] = [[-1.7, -1.4], [1.8, -1.5], [1.95, 1.2], [0.2, 1.75], [-1.9, 1.1]];
      return json({
        fence_half_extent_m: 2,
        predicted: {
          z_min_m: 0.1, z_max_m: 1.0,
          slices: [0.1, 0.55, 1.0].map((z) => ({ z_m: z, outline: reach })),
          everywhere: { shape: "polygon", vertices: reach, z_min: 0.1, z_max: 1.0 },
        },
      });
    }
    if (path.startsWith("/history/flights/") && path.endsWith("/playback")) return json(samplePlayback);
    if (path === "/history/flights") return json(sampleFlights);
    if (path.startsWith("/missions/")) return json(missions.find((m) => path.endsWith(m.id)) ?? sampleMission);
    return realFetch(input, init);
  };
}

/**
 * The console's worst case: the longest refusal the agent actually sends, a
 * check detail that wraps, and enough lines to make the pane scroll.
 */
const logLines: LogLine[] = [
  { id: 1, at: Date.now() - 42000, kind: "link", text: "Flight agent connected" },
  { id: 2, at: Date.now() - 38000, kind: "command", text: "Start session" },
  { id: 3, at: Date.now() - 37000, kind: "state", text: "idle → starting" },
  { id: 4, at: Date.now() - 36000, kind: "ok", text: "Radio: passed", detail: "cf-4a3b2c1d0e9f8a7b answered" },
  { id: 5, at: Date.now() - 35000, kind: "ok", text: "Battery: passed", detail: "3.82 V — the drone reports it can fly" },
  {
    id: 6, at: Date.now() - 34000, kind: "refused", text: "Positioning: failed",
    detail: "Only 0 of 4 base stations are being received. Stored geometry is not signal — re-run geometry estimation in cfclient before flying.",
  },
  { id: 7, at: Date.now() - 33000, kind: "state", text: "starting → checks_failed" },
  { id: 8, at: Date.now() - 20000, kind: "command", text: "Battery & motor test" },
  { id: 9, at: Date.now() - 12000, kind: "refused", text: "Battery & motor test: did not pass", detail: "Motors 2, 3 failed" },
  { id: 10, at: Date.now() - 4000, kind: "flight", text: "takeoff", detail: "rising to 0.30 m above the captured ground" },
];

/**
 * Every page is measured inside the REAL shell now — sidebar, flight strip and
 * all. Measuring a page on its own was fine when the chrome was a single bar;
 * with a 15rem rail taken out of the width it would measure a window nobody
 * runs.
 */
/**
 * `?only=<substring>` renders just the sections whose label matches.
 *
 * The harness stacks every page, so a screenshot captures whichever one happens
 * to be at the top. Isolating a section is how a canvas — the room maps, which
 * the DOM measurement cannot see into — gets looked at at all.
 */
const only = query.get("only")?.toLowerCase();

function Shell({ label, children }: { label: string; children: React.ReactNode }) {
  if (only && !label.toLowerCase().includes(only)) return null;
  return (
    <section className="border-b-4 border-[var(--primary)]">
      <p className="bg-[var(--primary)] px-3 py-1 text-xs font-bold text-[var(--on-primary)]">{label}</p>
      <div className="flex h-[900px] bg-[var(--background)] text-[var(--foreground)]">
        <Sidebar
          page="control"
          onNavigate={() => {}}
          session={session}
          starting={false}
          flying={false}
          mode={session.mode}
          onSetMode={() => {}}
          onSignOut={() => {}}
        />
        <div className="flex min-w-0 flex-1 flex-col">
          <FlightStrip
            session={session}
            sync={{ pending_flights: 2, pending_events: 5, uploading: null, last_error: null, everything_sent: false,
                    storage_error: null, storage_full: false }}
            connected
            flying={false}
            inSession
            onLand={() => {}}
            onStop={() => {}}
            onEndSession={() => {}}
          />
          <main className="console-scroll min-w-0 flex-1 overflow-y-auto">
            <div className="grid gap-5 px-4 py-5">{children}</div>
          </main>
        </div>
      </div>
    </section>
  );
}

/** Manual in the lab: both stations measured and heard, the drone turned 35°,
 *  and a minute of flying behind it — the room map's trail. */
const twoStations: Record<string, number> = {
  ...values, "stabilizer.yaw": 35, "stateEstimate.x": 0.6, "stateEstimate.y": 0.2,
  "lighthouse.bsReceive": 0b0011, "lighthouse.bsGeoVal": 0b0011, "lighthouse.bsCalVal": 0b0011,
  "lighthouse.width0": 210, "lighthouse.width1": 204, "lighthouse.width2": 198, "lighthouse.width3": 201,
};
const manualTelemetry: Telemetry = { values: twoStations, height_m: 0.55, positioned: true, at: 0 };
const manualHistory: History = Array.from({ length: 160 }, (_, i) => {
  const a = i / 40;
  return { t: i / 10, height_m: 0.3 + i / 640,
           values: { ...twoStations, "stateEstimate.x": 0.6 * Math.sin(a) + i / 400 - 0.4 + 0.4,
                     "stateEstimate.y": -0.9 + i / 120 * 0.8 } };
});

const control = (label: string, override: Partial<Session>, autoStart?: FlowStart,
                 live: { telemetry: Telemetry; history: History } = { telemetry, history }) => (
  <Shell label={label}>
    <ControlPage
      autoStart={autoStart}
      session={{ ...session, ...override }}
      telemetry={live.telemetry}
      history={live.history}
      intent={{ ...EMPTY_INTENT, up: true, yaw_left: true }}
      flying={false}
      run={runHere}
      logLines={logLines}
      onClearLog={() => {}}
      onOpenSessions={() => {}}
    />
  </Shell>
);

/** Set up step 2: the drone as it reads after its AI deck was swapped, and a
 *  fully installed drone opened to reinstall a part. */
const setupBase: Omit<SetupState, "phase" | "parts" | "facts" | "message"> = {
  current: null, progress: 0, order: ["main", "lighthouse", "camera", "wifi"],
  labels: { main: "Drone firmware", lighthouse: "Positioning deck", camera: "Camera", wifi: "Camera Wi-Fi" },
};
const setupSwapped: SetupState = {
  ...setupBase, phase: "ready", message: null,
  facts: { battery_v: 4.12, ai_deck: true, deck_unrecorded: true },
  parts: { main: "installed", lighthouse: "installed", camera: "needed", wifi: "needed" },
};
const setupDone: SetupState = {
  ...setupBase, phase: "done", message: "This drone's software is already installed.",
  facts: { battery_v: 4.12, ai_deck: true, deck_unrecorded: false },
  parts: { main: "installed", lighthouse: "installed", camera: "installed", wifi: "installed" },
};
const setupCommand = {
  command: "/Applications/DroneDeck.app/Contents/MacOS/dronedeck-agent setup",
  check: "/Applications/DroneDeck.app/Contents/MacOS/dronedeck-agent setup --check",
};
/** Drone Wi-Fi: a deck joined at the lab's weak signal, its radio held at
 *  reduced power after a brownout (wifi-link.patch). */
const wifiBrownout: CameraWifi = {
  ssid: "VT Open WiFi", phase: "joined", ip: "100.96.55.59", rssi: -86, drops: 0,
  message: "On VT Open WiFi at 100.96.55.59 · -86 dBm (weak). Its radio is on reduced power since a brownout — unplug the battery for 10 seconds to clear it.",
  link: { bandwidth_mhz: 20, power_dbm: 20, beacon_timeout_s: 15, brownout: true },
};
const wifiFull: CameraWifi = {
  ...wifiBrownout, message: "On VT Open WiFi at 100.96.55.59 · -86 dBm (weak).",
  link: { bandwidth_mhz: 20, power_dbm: 20, beacon_timeout_s: 15, brownout: false },
};
const setupSession: Session = { ...session, session_id: null, radio: { state: "searching", message: "Looking for the drone…", hardware_id: null } };

createRoot(document.getElementById("root")!).render(
  <div className="bg-[var(--background)] text-[var(--foreground)]">
    <Shell label="Startup"><StartupPage connected={false} /></Shell>
    <Shell label="Control · Auto page while a Manual session is open">
      <ModeLock session={{ ...session, mode: "manual", state: "ready" }} looking="auto" onBack={() => {}} />
    </Shell>
    <Shell label="Set up · new AI deck">
      <SetupPage session={setupSession} setup={setupSwapped} wifi={null} run={runHere} onGo={() => {}} />
    </Shell>
    <Shell label="Drone Wi-Fi · deck radio after a brownout">
      <WifiPage wifi={wifiBrownout} session={setupSession} onSaved={() => {}} />
    </Shell>
    <Shell label="Drone Wi-Fi · deck radio at full power">
      <WifiPage wifi={wifiFull} session={setupSession} onSaved={() => {}} />
    </Shell>
    <Shell label="Set up · install by command">
      <Install setup={setupSwapped} run={runHere} busy={false} onClose={() => {}} command={setupCommand} />
    </Shell>
    <Shell label="Set up · installed, reinstall">
      <Install setup={setupDone} run={runHere} busy={false} onClose={() => {}} command={setupCommand} />
    </Shell>
    <Shell label="Home">
      <HomePage session={session} telemetry={telemetry} sync={null} connected run={noop} onGo={() => {}} />
    </Shell>
    {control("Control · awaiting confirmation", { state: "awaiting_confirmation" })}
    {control("Control · retry required", {
      state: "ready", retry_required: true,
      message: "The drone reports it has tumbled — motors stopped. The flight ended early — press Retry to check the drone again before flying.",
    })}
    {control("Control · Auto, ready", { state: "ready", mode: "manual", assisted: true, message: null })}
    {control("Auto · ① Mission list", { state: "idle", mode: "auto", assisted: true, message: null, checks: [], health_test: null })}
    {control("Auto · ① Mission editor", { state: "idle", mode: "auto", assisted: true, message: null, checks: [], health_test: null },
             { step: 1, missionId: "m-long", view: "edit" })}
    {control("Auto · ② Check", { state: "awaiting_confirmation", mode: "auto", assisted: true, message: null },
             { step: 4, missionId: "m-long" })}
    {control("Auto · ③ Fly, flying", {
      state: "busy", mode: "auto", activity: "mission", assisted: true, message: "Holding at P2",
      mission: {
        id: "m-long", name: sampleMission.name, revision: 2, room_id: "lab", points: ["P1", "P2", "P3", "P4"],
        state: "holding", current_point_id: "P2", completed_point_ids: ["P1"],
        last_event: { kind: "hold_started", at_s: 31.2, point_id: "P2", detail: "Holding at P2 for 5 s — 4 cm off the point" },
      },
      processing: {
        on: true, chosen: false, last_flight_id: null, jobs: [],
        live: { flight_id: "f-air", points: {
          P1: { state: "done", verdict: "anomaly", reasons: ["Warmer than expected near P1"],
                findings: [{ id: "x1", title: "Warmer than expected near P1", severity: "warning",
                             sentence: "From 0:11 to 0:15, near P1, the temperature sensor read 3.8 °C above the drone's normal cooling curve.", signal: "temperature" }] },
          P2: { state: "running" },
        } },
      },
    }, { step: 5, missionId: "m-long" })}
    {control("Auto · ③ Fly, landed", {
      state: "ready", mode: "auto", activity: null, assisted: true, message: null,
      mission: {
        id: "m-long", name: sampleMission.name, revision: 2, room_id: "lab", points: ["P1", "P2", "P3"],
        state: "done", current_point_id: null, completed_point_ids: ["P1", "P2", "P3"],
        last_event: { kind: "done", at_s: 88.0, point_id: null, detail: "Mission complete" },
      },
    }, { step: 5, missionId: "m-long" })}
    {control("Auto · full screen editor", { state: "idle", mode: "auto", assisted: true, message: null, checks: [], health_test: null },
             { step: 1, missionId: "m-long", view: "edit", full: true })}
    {control("Control · Manual, armed", {
      state: "busy", mode: "manual", activity: "manual", assisted: true, message: null,
    })}
    {control("Control · Manual, arrows from you", {
      state: "busy", mode: "manual", activity: "manual", assisted: true, message: null,
      controls: { key_frame: "operator", operator: [0.4, -1.8], operator_marked_at: "2026-09-30T09:00:00+00:00",
                  live: { chosen: "operator", active: "operator", reason: null, forward_deg: 72.5,
                          operator: [0.4, -1.8], operator_source: "marked" } },
    })}
    {control("Control · Manual, arrows near you", {
      state: "busy", mode: "manual", activity: "manual", assisted: true, message: null,
      controls: { key_frame: "operator", operator: [0.4, -1.8], operator_marked_at: "2026-09-30T09:00:00+00:00",
                  live: { chosen: "operator", active: "room", reason: "near_operator", forward_deg: 0,
                          operator: [0.4, -1.8], operator_source: "marked" } },
    })}
    {control("Control · Manual, arrows no position", {
      state: "busy", mode: "manual", activity: "manual", assisted: false, message: null,
      controls: { key_frame: "room", operator: null, operator_marked_at: null,
                  live: { chosen: "room", active: "takeoff", reason: "no_position", forward_deg: -116,
                          operator: null, operator_source: null } },
    })}
    {control("Control · Manual, the room from the stations, flying", {
      state: "busy", mode: "manual", activity: "manual", assisted: true, message: null,
      controls: { key_frame: "operator", operator: [0.4, -1.8], operator_marked_at: "2026-09-30T09:00:00+00:00",
                  live: { chosen: "operator", active: "operator", reason: null, forward_deg: 72.5,
                          operator: [0.4, -1.8], operator_source: "marked" } },
    }, undefined, { telemetry: manualTelemetry, history: manualHistory })}
    {control("Control · Manual, the room from the stations, on the ground", {
      state: "ready", mode: "manual", activity: null, assisted: true, message: null,
      controls: { key_frame: "room", operator: null, operator_marked_at: null, live: null },
    }, undefined, { telemetry: { ...manualTelemetry, height_m: 0.0 }, history: [] })}
    {control("Control · Manual, not placed — no light", {
      state: "awaiting_confirmation", mode: "manual", assisted: false,
    }, undefined, { telemetry: { values: { ...twoStations, "lighthouse.bsReceive": 0, "lighthouse.width0": 0,
      "lighthouse.width1": 0, "lighthouse.width2": 0, "lighthouse.width3": 0, "stabilizer.yaw": 20 },
      height_m: null, positioned: false, at: 0 }, history: [] })}
    {control("Control · Manual, unassisted, look again", {
      state: "awaiting_confirmation", mode: "manual", assisted: false,
    })}
    {control("Control · Manual, no position, on the ground", {
      state: "ready", mode: "manual", activity: null, assisted: false, message: null,
      controls: { key_frame: "operator", operator: null, operator_marked_at: null, live: null },
    })}
    {WINDOWS.map((w) => (
      <Shell key={w.key} label={`Window · ${w.label}`}>
        <SensorWindow windowKey={w.key} telemetry={telemetry} history={history} session={session} onOpenLog={() => {}} />
      </Shell>
    ))}
    {!agentToken && (
      <>
        <Shell label="Capture · in a capture session">
          <CapturePage session={{ ...session, state: "ready", session_id: "3f1c", message: null, purpose: "capture",
                                  radio: { state: "connected", hardware_id: "cf-4a3b2c1d0e9f8a7b", message: null } }}
                       run={runHere} onOpenControl={() => {}} />
        </Shell>
        <Shell label="Capture · no session">
          <CapturePage session={{ ...session, state: "idle", session_id: null, message: null }} run={runHere} onOpenControl={() => {}} />
        </Shell>
        <Shell label="Playback · the page, all flights">
          <PlaybackPage refreshKey="h" mode={(query.get("mode") as "auto" | "manual") ?? "auto"} />
        </Shell>
        <Shell label="Playback · mid-flight">
          <FlightPlaybackView flightId="f1c2d3e4-playback" title={sampleMission.name} onBack={() => {}} startAt={33} />
        </Shell>
        <Shell label="Playback · full screen">
          <FlightPlaybackView flightId="f1c2d3e4-playback" title={sampleMission.name} onBack={() => {}} startAt={18} startFullScreen />
        </Shell>
      </>
    )}
    {agentToken && (
      <>
        <Shell label="Window log · Flight">
          <WindowLog windowKey="flight" refreshKey="h" mode={(query.get("mode") as "auto" | "manual") ?? "manual"} onBack={() => {}} />
        </Shell>
        <Shell label="Sessions">
          <SessionsPage refreshKey="h" mode={(query.get("mode") as "auto" | "manual") ?? "manual"} selectedId={query.get("session")} onSelect={() => {}} />
        </Shell>
        {/* `?edit=<mission id>`: the real editor on the real agent — edit, save
            and reopen a saved mission end to end (2026-10-25). */}
        <Shell label="Plan · editor on the agent">
          <MissionStep run={runHere} telemetry={telemetry} selectedId={null} onUse={() => {}}
                       openEditor={query.get("edit") ?? undefined} />
        </Shell>
      </>
    )}
  </div>,
);

// `?measure=1` walks the rendered DOM for anything crossing the viewport and
// writes the verdict into the page, where --dump-dom can read it. Two frames,
// so React has committed and the canvas has had its first ResizeObserver pass.
if (query.has("measure")) {
  requestAnimationFrame(() => requestAnimationFrame(() => reportInto("measure-report")));
}
