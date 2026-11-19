/**
 * The DroneDeck window.
 *
 * A left sidebar for navigation, a strip across the top for flight state and
 * stopping, then one page at a time: Home (where you are and what to do next),
 * Control (what you do), and the live sensor windows (what the drone reports).
 * Home opens first — an operator who has just installed this needs the next
 * step, not eight tabs.
 *
 * The shell is copy-adapted from the web app's operator view, so the two read
 * as one product. The split between the two surfaces is not cosmetic:
 *
 * - **The sidebar navigates.** Pages, the Auto/Manual mode, the account.
 * - **The strip stops things.** Land, Emergency stop, End session, the sync
 *   state. A nav rail scrolls; an abort control must never be scrollable out
 *   of reach, which is why these two are separate surfaces.
 *
 * Two controls are always present while a session is running, because the
 * moment you need them is the moment something is going wrong:
 *
 * - **Land** — the normal way to end a flight, and the keyboard's `L`.
 * - **Emergency stop** — hold for a second. No keyboard shortcut: cutting the
 *   motors in the air drops the drone, so it must be deliberate.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AgentError,
  EMPTY_INTENT,
  LiveConnection,
  agentLogPath,
  api,
  connectToShell,
  watchAgentExit,
  type Intent,
  type Mode,
  type SetupState,
  type Session,
  type SyncStatus,
  type Telemetry,
} from "@/lib/agent";
import { useCommandLog, useSessionNarration, useStationNarration } from "@/lib/commandLog";
import { ControlPage } from "@/pages/control/ControlPage";
import { ModeLock } from "@/pages/control/ModeLock";
import { sessionOpen, shownMode } from "@/lib/sessionMode";
import { HomePage } from "@/pages/home/HomePage";
import { FlightStrip } from "@/components/FlightStrip";
import { Sidebar } from "@/components/Sidebar";
import { SessionsPage } from "@/pages/sessions/SessionsPage";
import { PlaybackPage } from "@/pages/playback/PlaybackPage";
import { WindowLog } from "@/pages/sessions/WindowLog";
import { StartupPage } from "@/pages/startup/StartupPage";
import { SensorWindow, type WindowKey } from "@/pages/windows/SensorWindow";
import { Message } from "@/components/ui";
import { SetupPage } from "@/pages/setup/SetupPage";
import { intentFromKeys, isShift, KEY_MAP, sameIntent } from "@/lib/keys";

export { KEY_MAP } from "@/lib/keys";

export const HISTORY_S = 60;
const HISTORY_LIMIT = HISTORY_S * 10;      // 10 Hz

export type History = { t: number; values: Record<string, number>; height_m: number | null }[];

export type Page = "home" | "setup" | "control" | "sessions" | "playback" | WindowKey;

/** A command, and what to call it in the log. */
export type Run = (action: () => Promise<unknown>, label?: string) => Promise<void>;

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [sync, setSync] = useState<SyncStatus | null>(null);
  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [history, setHistory] = useState<History>([]);
  const [connected, setConnected] = useState(false);
  const [page, setPageState] = useState<Page>("home");
  // Drill-downs within a page: one session's detail, or a window's history log.
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [logWindow, setLogWindow] = useState<WindowKey | null>(null);
  const setPage = useCallback((next: Page) => {
    setPageState(next);
    setSessionId(null);
    setLogWindow(null);
  }, []);
  const [error, setError] = useState<string | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const [intent, setIntent] = useState<Intent>(EMPTY_INTENT);
  /** With a session open: the other mode's page, if the operator chose to look at it. */
  const [lookingAt, setLookingAt] = useState<Mode | null>(null);
  // Set up's progress, live from the agent (drone_setup.py).
  const [setupState, setSetupState] = useState<SetupState | null>(null);

  const live = useRef<LiveConnection | null>(null);
  const startedAt = useRef<number | null>(null);
  // Why the agent is not running, from the shell (lib.rs), and where its log
  // is. Before these, a dead agent was only ever "Restart DroneDeck".
  const [agentExit, setAgentExit] = useState<string | null>(null);
  const [logPath, setLogPath] = useState<string | null>(null);

  // What the window did and what the agent said back. Observation only — see
  // lib/commandLog.ts. It is not the agent's audit trail.
  const { lines: logLines, push: pushLog, clear: clearLog } = useCommandLog();
  useSessionNarration(session, connected, pushLog);
  // Stations heard or lost, as one line each (commandLog.ts).
  useStationNarration(telemetry, pushLog);

  // ── connect to the agent ───────────────────────────────────────────
  useEffect(() => {
    let connection: LiveConnection | null = null;
    void (async () => {
      await connectToShell();
      connection = new LiveConnection({
        onSession: setSession,
        onRefused: setRefused,
        onSync: setSync,
        onConnection: setConnected,
        onSetup: setSetupState,
        onTelemetry: (frame) => {
          setTelemetry(frame);
          if (startedAt.current === null) startedAt.current = frame.at;
          setHistory((previous) => {
            const next = [...previous, {
              t: frame.at - (startedAt.current ?? frame.at),
              values: frame.values,
              height_m: frame.height_m,
            }];
            return next.length > HISTORY_LIMIT ? next.slice(-HISTORY_LIMIT) : next;
          });
        },
      });
      connection.open();
      live.current = connection;
    })();
    return () => connection?.close();
  }, []);

  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | undefined;
    watchAgentExit(setAgentExit)
      .then((stop) => { if (cancelled) stop(); else unlisten = stop; })
      .catch(() => undefined);
    agentLogPath().then((path) => { if (!cancelled) setLogPath(path); }).catch(() => undefined);
    return () => { cancelled = true; unlisten?.(); };
  }, []);

  const signedIn = Boolean(session?.operator);

  // Set up's state survives a window reload: ask once on (re)connecting.
  useEffect(() => {
    if (!connected || !signedIn) return;
    api.setupState().then(setSetupState).catch(() => undefined);
  }, [connected, signedIn]);

  // Vitals are only true while the radio is up. Without this the last frame
  // stayed on every sensor page marked "Live" after the drone switched off
  // (2026-09-24).
  const radioState = session?.radio?.state;
  useEffect(() => {
    if (radioState !== undefined && radioState !== "connected") setTelemetry(null);
  }, [radioState]);

  /**
   * Run a command, showing its refusal verbatim if it declines.
   *
   * `label` is optional and only used for the console: adding it changed no
   * call site, and the error path below is exactly as it was.
   */
  const run = useCallback<Run>(async (action, label) => {
    setError(null);
    if (label) pushLog("command", label);
    try {
      await action();
      if (label) pushLog("ok", `${label} accepted`);
    } catch (e) {
      const message = e instanceof AgentError ? e.message : "Something went wrong.";
      setError(message);
      pushLog("refused", label ? `${label} refused` : "Command refused", message);
    }
  }, [pushLog]);

  // A mission is flying too (2026-09-29): without it the strip offered End
  // session, not Land and Emergency stop, while a mission was in the air.
  const flying = session?.activity === "manual" || session?.activity === "program"
    || session?.activity === "mission";
  const manualFlying = session?.activity === "manual";
  // The flight keys physically held, and whether Shift is — the intent sent is
  // worked out from these (lib/keys.ts), so Shift can flip a held arrow.
  const heldKeys = useRef<Set<string>>(new Set());
  const reversed = useRef(false);

  // ── keyboard: key *state*, not the OS repeat stream ────────────────
  useEffect(() => {
    const held = heldKeys.current;
    // Send what the held keys now ask for — only when that changed.
    const apply = () => {
      const next = intentFromKeys(held, reversed.current);
      setIntent((current) => {
        if (sameIntent(current, next)) return current;
        live.current?.sendIntent(next);
        return next;
      });
    };

    const onDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;

      // Space does nothing — including activating a focused button, which is
      // how it could otherwise stop the motors by accident (WebKit activates
      // on keyup, so that is blocked below too).
      if (event.code === "Space") {
        event.preventDefault();
        return;
      }
      if (event.code === "KeyL") {
        event.preventDefault();
        void run(api.land, "Land (L)");
        return;
      }
      // Shift reverses the arrows while held (lib/keys.ts) — a held ↑ flies
      // ↓ the instant it goes down. Never a command of its own.
      if (isShift(event.code)) {
        reversed.current = true;
        apply();
        return;
      }
      if (event.repeat || !manualFlying) return;

      const field = KEY_MAP[event.code];
      if (!field) return;
      event.preventDefault();
      reversed.current = event.shiftKey;
      held.add(event.code);
      apply();
    };

    const onUp = (event: KeyboardEvent) => {
      if (event.code === "Space") {
        event.preventDefault();
        return;
      }
      if (isShift(event.code)) {
        reversed.current = false;
        apply();
        return;
      }
      if (!KEY_MAP[event.code]) return;
      event.preventDefault();
      held.delete(event.code);
      apply();
    };

    // A window that loses focus never sees keyup. Releasing everything is the
    // safe reading: the drone holds its height instead of drifting on.
    const onBlur = () => {
      held.clear();
      reversed.current = false;
      apply();
    };

    window.addEventListener("keydown", onDown);
    window.addEventListener("keyup", onUp);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onDown);
      window.removeEventListener("keyup", onUp);
      window.removeEventListener("blur", onBlur);
    };
  }, [manualFlying, run]);

  // Nothing is known yet: the agent has not answered, or it is still trying the
  // saved sign-in. Hold the shape rather than claim "signed out".
  const starting = session === null || session.restoring;
  const inSession = session != null &&
    !["signed_out", "idle"].includes(session.state);
  // History reloads whenever a session starts or ends.
  const historyKey = `${session?.session_id ?? "none"}:${session?.state ?? ""}`;
  // The Auto/Manual toggle also chooses which history every page shows. The
  // fallback matches the AGENT's own default (session.py) so the filter does
  // not flip under the operator when the first frame lands.
  // The page shown follows the session's mode — unless, with a session open,
  // the operator chose to look at the other one (lib/sessionMode.ts).
  const viewMode: Mode = shownMode(session, lookingAt);
  const open = sessionOpen(session);
  // A session that ends forgets where the operator was looking: the next one
  // opens on its own mode's page.
  useEffect(() => { if (!open) setLookingAt(null); }, [open]);

  return (
    <div className="flex h-screen bg-[var(--background)] text-[var(--foreground)]">
      <Sidebar
        page={page}
        onNavigate={setPage}
        session={session}
        starting={starting}
        // Hover-to-expand is suppressed while flying: expanded, the rail covers
        // the Control page's console, which is what the operator is watching.
        flying={Boolean(flying)}
        mode={viewMode}
        onSetMode={(mode) => {
          // A session keeps its mode until it ends: the switch then only
          // changes the page you look at. With none open it sets the mode the
          // next session opens in.
          if (open) setLookingAt(mode === session?.mode ? null : mode);
          else void run(() => api.setMode(mode), `Set mode to ${mode}`);
        }}
        onSignOut={() => void run(api.signOut, "Sign out")}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <FlightStrip
          session={session}
          sync={sync}
          connected={connected}
          flying={Boolean(flying)}
          inSession={inSession}
          onLand={() => void run(api.land, "Land")}
          onStop={() => void run(api.emergencyStop, "Emergency stop")}
          onEndSession={() => void run(api.endSession, "End session")}
        />

        {(!connected || error) && (
          <div className="grid gap-2 border-b border-[var(--border)] bg-[var(--surface)] px-4 py-2">
            {!connected && (
              <Message
                tone="critical"
                text={agentExit
                  ? `Not connected to the flight agent on this computer. ${agentExit}` +
                    (logPath ? ` Log: ${logPath}` : "")
                  : "Not connected to the flight agent on this computer."}
              />
            )}
            {error && <Message tone="critical" text={error} />}
          </div>
        )}

        <main className="console-scroll min-w-0 flex-1 overflow-y-auto">
          <div className="grid gap-5 px-4 py-5">
            {refused ? (
              <Message tone="critical" text={refused} />
            ) : starting ? (
              <StartupPage connected={connected} />
            ) : page === "home" ? (
              <HomePage
                session={session}
                telemetry={telemetry}
                sync={sync}
                connected={connected}
                run={run}
                onGo={setPage}
              />
            ) : page === "control" && open && session && viewMode !== session.mode ? (
              <ModeLock session={session} looking={viewMode} onBack={() => setLookingAt(null)} />
            ) : page === "control" ? (
              <ControlPage
                session={session}
                telemetry={telemetry}
                history={history}
                intent={intent}
                flying={Boolean(flying)}
                run={run}
                logLines={logLines}
                onClearLog={clearLog}
                onOpenSessions={() => setPage("sessions")}
              />
            ) : page === "setup" ? (
              <SetupPage session={session} setup={setupState} run={run} onGo={setPage} />
            ) : page === "sessions" ? (
              <SessionsPage refreshKey={historyKey} mode={viewMode} selectedId={sessionId} onSelect={setSessionId} />
            ) : page === "playback" ? (
              <PlaybackPage refreshKey={historyKey} mode={viewMode} />
            ) : logWindow === page ? (
              <WindowLog windowKey={page} refreshKey={historyKey} mode={viewMode} onBack={() => setLogWindow(null)} />
            ) : (
              <SensorWindow
                windowKey={page}
                telemetry={telemetry}
                history={history}
                session={session}
                onOpenLog={() => setLogWindow(page)}
              />
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
