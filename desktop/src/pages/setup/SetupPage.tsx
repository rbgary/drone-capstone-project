/**
 * Set up — from a drone out of the box to a session.
 *
 * ONE LIST, ONE STEP OPEN. The step in front of the operator is open; finished
 * steps shrink to a line with a check; later steps are titles only. Words are
 * instructions, not explanations — the why lives in the docs (setup.txt), not
 * on the screen.
 *
 * Step 2 is the agent's drone_setup.py, RUN IN A TERMINAL — there is no
 * install button (2026-10-24, Samuel: "i dont want people accidentally
 * pressing a button to reinstall a firmware"). It shows lines to copy:
 * install what is missing, reinstall one part (--reinstall), and read only
 * (--check) — this machine's exact command from GET /setup/command. DroneDeck
 * is quit first: the radio has one owner, and while the app is open its agent
 * holds it. The engine reads what the drone already has (never assumes),
 * writes drone firmware, the positioning deck, camera and camera Wi-Fi in that
 * order, asks for the battery unplug the camera needs, and calls it done only
 * once the camera has started. The page's one button, Read what is installed,
 * changes nothing.
 *
 * Step 2 leads the list only while reading, after a failed read, or with parts
 * missing; its button opens it any time. A replaced AI deck is told apart by
 * its factory serial and reads as not installed (setup.txt).
 *
 * Step 1 says why the drone cannot be reached in the agent's words, with the
 * three fixes in the order they work; standby keeps trying by itself.
 *
 * Step 3 sets each base station's channel over its USB cable (agent
 * flight/basestation.py — Bitcraze's "Set BS channel"): every V2 station must
 * be on its own channel, 1 and 2 for a pair, or the drone reads the light and
 * the calibration but decodes no angles (measured 2026-10-05). It reads done
 * once the drone decodes angles.
 *
 * Measuring where the base station stands is NOT here: it is part of flying,
 * Control › Auto › ② Position, where the operator already is (2026-10-05 —
 * it sat here as step 4 and could not be found).
 *
 * "Connect the camera to Wi-Fi" was step 4 until 2026-11-15: the drone carries
 * no camera now. Step 2 still installs the AI deck's parts on a drone that has
 * one — that goes in the no-camera work's firmware phase.
 */

import { useEffect, useState, type ReactNode } from "react";
import type { Page, Run } from "@/App";
import {
  AgentError, api, type BaseStation, type Session, type SetupCommand,
  type SetupState, type StationStatus,
} from "@/lib/agent";
import { StationStages, useStation } from "@/components/StationStages";
import { Button, Message, PageHeader, Spinner, StatusDot, type Tone } from "@/components/ui";

type Step = "connect" | "install" | "channels" | "session";

export function SetupPage({ session, setup, run, onGo }: {
  session: Session | null;
  setup: SetupState | null;
  run: Run;
  onGo: (page: Page) => void;
}) {
  const radio = session?.radio?.state;
  const installed = setup?.phase === "done";
  const connected = radio === "connected" || (setup !== null && setup.phase !== "idle"
    && setup.phase !== "failed" && setup.facts !== null);
  const inSession = session?.session_id != null;
  const station = useStation(radio === "connected");
  // A step the operator opened by its button, over the one the list chose.
  const [opened, setOpened] = useState<Step | null>(null);
  // Decoding: the drone turns sweeps into angles — what the channels are for.
  // bsReceive is the firmware's own word for it (lighthouse_core.c sets it
  // only when a sweep yields angles); validAngles is not (2026-10-05).
  const decoding = station?.connected === true && station.received.length > 0
    && opened !== "channels";
  // Step 2 is always open (its commands are always on the page), so it never
  // takes the list's place: the list moves on from connecting to the
  // channels, whatever the drone has installed.
  const listed: Step = !connected ? "connect"
    : !decoding ? "channels" : "session";
  const current: Step = connected && opened !== null ? opened : listed;
  const done = (s: Step) => ({
    connect: connected, install: installed, channels: decoding, session: inSession,
  })[s];

  return (
    <div className="grid max-w-3xl gap-4">
      <PageHeader eyebrow="Operate" title="Set up" />
      <ol className="grid gap-2">
        <Item n={1} title="Connect the drone" done={done("connect")} open={current === "connect"}
              summary="Crazyradio plugged in, drone on">
          <Connect session={session} />
        </Item>

        {/* ALWAYS OPEN, NO BUTTON IN ITS HEADER (2026-10-24, Samuel): the
            header's "Install or reinstall" only opened the step, but it read as
            an install button, and the commands hid behind it. The commands are
            on the page whatever step is current. */}
        <Item n={2} title="Install drone software — from Terminal" done={done("install")} open
              summary="Drone firmware, positioning deck, camera and camera Wi-Fi installed">
          <Install setup={setup} run={run} busy={inSession} />
        </Item>

        <Item n={3} title="Base stations: one channel each" done={done("channels")} open={current === "channels"}
              summary="The drone decodes the base station's sweeps"
              onReopen={() => setOpened("channels")} reopenLabel={done("channels") ? "Check again" : "Set"}>
          <Channels station={station} run={run} onDone={() => setOpened(null)} />
        </Item>

        <Item n={4} title="Fly" done={done("session")} open={current === "session"}
              summary="Session running">
          <p className="text-sm">The drone is set up. In Control › Auto, ② Position measures where the base station stands from where the drone sits, then the flyable space, the plan, and Fly.</p>
          <div><Button variant="primary" onClick={() => onGo("control")}>Go to Control</Button></div>
        </Item>
      </ol>
    </div>
  );
}

/** The install command for this machine, from the agent: loading, failed with
 *  a retry, or the line. */
function useSetupCommand(given?: SetupCommand) {
  const [cmd, setCmd] = useState<SetupCommand | null>(given ?? null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (given || cmd) return;
    let live = true;
    api.setupCommand().then(
      (c) => { if (live) { setCmd(c); setFailed(false); } },
      () => { if (live) setFailed(true); },
    );
    // The agent may still be starting when the page opens: keep asking, so
    // the commands appear by themselves rather than behind a button.
    const timer = window.setTimeout(() => setAttempt((n) => n + 1), 3000);
    return () => { live = false; window.clearTimeout(timer); };
  }, [given, cmd, attempt]);
  return { cmd, failed, retry: () => setAttempt((n) => n + 1) };
}

/** One line to paste into a terminal, shown whole (it wraps, never cut) with a
 *  Copy button. */
function CommandBox({ line }: { line: string }) {
  const [copied, setCopied] = useState<"yes" | "no" | null>(null);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(null), 2500);
    return () => clearTimeout(t);
  }, [copied]);
  const copy = () => navigator.clipboard.writeText(line).then(() => setCopied("yes"), () => setCopied("no"));
  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-start gap-2 border border-[var(--border)] bg-[var(--surface-2)] p-3">
        <code className="min-w-0 flex-1 select-all wrap-anywhere font-mono text-sm">{line}</code>
        <Button onClick={() => void copy()}>{copied === "yes" ? "Copied" : "Copy"}</Button>
      </div>
      {copied === "no" && <Message tone="warning" text="Could not copy. Select the line and copy it." />}
    </div>
  );
}

/**
 * Step 1. What DroneDeck sees on the radio, in the agent's own words, and —
 * when it cannot reach the drone — the three things that fix it, in the order
 * they work. It keeps trying by itself (standby), so there is no button.
 */
function Connect({ session }: { session: Session | null }) {
  const radio = session?.radio;
  const trouble = radio?.state !== "connected" && radio?.message != null
    && radio.message !== "Looking for the drone…";
  return (
    <div className="grid gap-3">
      <p className="text-sm">Plug the Crazyradio into this laptop, then switch the drone on.</p>
      {trouble ? (
        <div className="grid gap-2 border-l-2 border-[var(--status-warning)] pl-3">
          <p className="text-sm"><StatusDot tone="warning">{radio?.message}</StatusDot></p>
          <ol className="grid gap-1 text-sm">
            <li><strong>1.</strong> Switch the drone off and on: unplug the battery for 10 seconds, then plug it back in.</li>
            <li><strong>2.</strong> Unplug the Crazyradio and plug it back in.</li>
            <li><strong>3.</strong> If it was just flown, charge the battery.</li>
          </ol>
          <p className="text-xs">DroneDeck tries again by itself every few seconds.</p>
        </div>
      ) : (
        <p className="text-sm"><StatusDot tone="idle">{radio?.message ?? "Looking for the drone…"}</StatusDot></p>
      )}
    </div>
  );
}

/** Reinstalling one part: what it is, and the --reinstall set that writes it.
 *  The AI deck's two chips are one bundle (drone_setup.reinstall_set), so
 *  they are one line. */
const REINSTALL: { label: string; parts: string; note: string }[] = [
  { label: "Drone firmware", parts: "main", note: "The drone's own software. No battery unplug." },
  { label: "Positioning deck", parts: "lighthouse", note: "The Lighthouse deck's image." },
  { label: "Camera and camera Wi-Fi", parts: "camera,wifi",
    note: "Both chips on the AI deck. It asks you to unplug the battery once." },
];

/**
 * Step 2's body; exported for the layout harness (harness.tsx), which passes
 * `command` because it has no agent to ask.
 *
 * COMMANDS ONLY (2026-10-24, Samuel: "i dont want people accidentally pressing
 * a button to reinstall a firmware — they should copy the command and run it
 * in the terminal"). Nothing here writes to the drone: every install and
 * reinstall is a line to copy, run with DroneDeck quit (the radio has one
 * owner). The one button reads what is installed and changes nothing.
 */
export function Install({ setup, run, busy, command }: {
  setup: SetupState | null; run: Run; busy: boolean; onClose?: () => void; command?: SetupCommand;
}) {
  const phase = setup?.phase ?? "idle";
  const checking = phase === "checking";
  const battery = setup?.facts?.battery_v;
  const { cmd, failed, retry } = useSetupCommand(command);
  const read = setup !== null && setup.order && Object.keys(setup.parts).length > 0;

  return (
    <div className="grid gap-4">
      <ol className="grid gap-1 text-sm">
        <li><strong>1.</strong> Quit DroneDeck (⌘Q) — while it is open it holds the radio.</li>
        <li><strong>2.</strong> Open Terminal (on Windows, PowerShell), paste a line below and press Return.</li>
        <li><strong>3.</strong> Keep the drone on. When it says so, unplug the battery for 10 seconds.</li>
        <li><strong>4.</strong> Open DroneDeck again and press Check what&apos;s on the drone.</li>
      </ol>

      {cmd === null ? (
        failed ? (
          <div className="flex flex-wrap items-center gap-2">
            <Message tone="critical" text="Could not get the commands from the flight agent." />
            <Button onClick={retry}>Try again</Button>
          </div>
        ) : <Spinner label="Getting the commands…" />
      ) : (
        <div className="grid gap-4">
          <CommandRow title="Install what is missing" line={cmd.command} main
                      note="Writes only what the drone lacks — about 5 minutes. Nothing it already has is touched." />
          <div className="grid gap-3">
            <p className="eyebrow">Reinstall one part</p>
            {REINSTALL.map((r) => (
              <CommandRow key={r.parts} title={r.label} note={r.note}
                          line={`${cmd.command} --reinstall ${r.parts}`} />
            ))}
          </div>
          <CommandRow title="Only read what is installed" line={cmd.check}
                      note="Changes nothing on the drone." />
        </div>
      )}

      <div className="grid gap-2 border-t border-[var(--border)] pt-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-semibold">On this drone</p>
          <Button disabled={busy || checking} onClick={() => void run(api.setupCheck, "Check what is on the drone")}>
            Check what&apos;s on the drone
          </Button>
        </div>
        <p className="text-xs">Only reads — it never writes to the drone.</p>
        {busy && <Message tone="warning" text="End the session first — reading the drone needs the radio." />}
        {checking && <Spinner label={setup?.message ?? "Reading the drone…"} />}
        {phase === "ready" && setup?.facts?.deck_unrecorded && (
          <Message tone="warning"
                   text="This AI deck is not recorded on this drone — a new deck, or one set up before decks were recorded. Install what is missing puts its camera and Wi-Fi software on it." />
        )}
        {read && !checking && (
          <ul className="grid border border-[var(--border)]">
            {setup.order.map((part) => (
              <li key={part} className="flex min-h-11 flex-wrap items-center justify-between gap-x-3 border-b border-[var(--border)] px-3 py-2 text-sm last:border-b-0">
                <span className="font-medium">{setup.labels[part]}</span>
                <PartStatus status={setup.parts[part]} />
              </li>
            ))}
          </ul>
        )}
        {!read && !checking && phase !== "failed" && (
          <p className="text-xs">Not read yet this time DroneDeck opened.</p>
        )}
        {battery != null && !checking && (
          <p className="text-xs">
            Battery {battery.toFixed(2)} V{battery < 3.8 ? " — charge it above 3.8 V before installing" : ""}
          </p>
        )}
        {phase === "failed" && setup?.message && <Message tone="critical" text={setup.message} />}
        {phase === "done" && setup?.message && <Message tone="good" text={setup.message} />}
      </div>
    </div>
  );
}

/** One command: what it does, then the line with its Copy button. */
function CommandRow({ title, note, line, main = false }: {
  title: string; note: string; line: string; main?: boolean;
}) {
  return (
    <div className={`grid gap-1.5 ${main ? "border-l-2 border-[var(--primary)] pl-3" : ""}`}>
      <p className={main ? "text-sm font-semibold" : "text-sm font-medium"}>{title}</p>
      <p className="text-xs">{note}</p>
      <CommandBox line={line} />
    </div>
  );
}

/** Each base station on its own channel, set over its USB cable. */
function Channels({ station, run, onDone }: {
  station: StationStatus | null; run: Run; onDone: () => void;
}) {
  const [found, setFound] = useState<BaseStation[] | null>(null);
  const [working, setWorking] = useState(false);
  const [outcome, setOutcome] = useState<{ tone: Tone; text: string } | null>(null);
  const scan = () => {
    setWorking(true);
    setOutcome(null);
    void run(async () => {
      try {
        setFound((await api.baseStations()).stations);
      } catch (e) {
        setOutcome({ tone: "critical", text: e instanceof AgentError ? e.message : "Could not look for base stations." });
        throw e;
      }
    }, "Look for base stations").finally(() => setWorking(false));
  };
  const setChannel = (port: string, channel: number) => {
    setWorking(true);
    void run(async () => {
      try {
        const r = await api.setBaseStationChannel(port, channel);
        setOutcome({ tone: "good", text: `Channel ${r.channel} set and saved. Unplug its USB cable, and do the other station.` });
        setFound((await api.baseStations()).stations);
      } catch (e) {
        setOutcome({ tone: "critical", text: e instanceof AgentError ? e.message : "The channel was not set." });
        throw e;
      }
    }, `Set base station channel ${channel}`).finally(() => setWorking(false));
  };
  const decoding = station?.connected === true && station.received.length > 0;
  return (
    <div className="grid gap-3">
      {station?.connected === true && <StationStages station={station} />}
      <ol className="grid gap-1 text-sm">
        <li>1. Keep the base station on its power block. Connect it to this laptop with a micro-USB cable — one station at a time.</li>
        <li>2. Press Look for base stations, then give the first station channel 1 and the second channel 2.</li>
        <li>3. Unplug the USB cables. The stations keep their channels.</li>
      </ol>
      {working && <Spinner label="Talking to the base station…" />}
      {outcome && !working && <Message tone={outcome.tone} text={outcome.text} />}
      {found !== null && found.length === 0 && !working && (
        <Message tone="warning" text="No base station on USB. Check the cable carries data (not only power) and the station is on." />
      )}
      {found !== null && found.length > 0 && (
        <ul className="grid border border-[var(--border)]">
          {found.map((s) => (
            <li key={s.port} className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] px-3 py-2 text-sm last:border-b-0">
              <span>
                <span className="mono">{s.serial_number ?? s.port}</span>{" · "}
                <StatusDot tone={s.supported ? "good" : "warning"}>
                  {s.channel == null ? "did not answer" : s.channel === 0 ? "channel 0 — the drone cannot decode it" : `channel ${s.channel}`}
                </StatusDot>
              </span>
              <span className="flex gap-2">
                <Button disabled={working} onClick={() => setChannel(s.port, 1)}>Channel 1</Button>
                <Button disabled={working} onClick={() => setChannel(s.port, 2)}>Channel 2</Button>
              </span>
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={working} onClick={scan}>Look for base stations</Button>
        {decoding && <Button onClick={onDone}>Decoding — continue</Button>}
      </div>
    </div>
  );
}

function PartStatus({ status }: { status: string | undefined }) {
  const [tone, label]: [Tone, string] =
    status === "installed" || status === "done" ? ["good", "Installed"]
    : status === "needed" ? ["warning", "Not installed"]
    : status === "absent" ? ["idle", "No deck fitted"] : ["idle", "—"];
  return <span className="text-xs"><StatusDot tone={tone}>{label}</StatusDot></span>;
}

function Item({ n, title, done, open, summary, children, onReopen, reopenLabel }: {
  n: number; title: string; done: boolean; open: boolean; summary: string; children: ReactNode;
  onReopen?: () => void; reopenLabel?: string;
}) {
  return (
    <li
      aria-current={open ? "step" : undefined}
      className={`border bg-[var(--surface)] ${open ? "border-[var(--primary)]" : "border-[var(--border)]"}`}
    >
      <div className="flex items-center gap-3 px-4 py-3">
        <span
          aria-hidden="true"
          className={`mono flex h-6 w-6 shrink-0 items-center justify-center border text-xs font-bold ${
            done ? "border-[var(--status-good)] text-[var(--status-good)]"
              : open ? "border-[var(--primary)] bg-[var(--primary)] text-[var(--on-primary)]"
              : "border-[var(--border)] text-[var(--muted)]"
          }`}
        >
          {done ? "✓" : n}
        </span>
        <span className="min-w-0 flex-1">
          <span className={`block text-sm font-semibold ${!open && !done ? "text-[var(--muted)]" : ""}`}>{title}</span>
          {done && !open && <span className="block text-xs text-[var(--muted)]">{summary}</span>}
        </span>
        {done && <span className="sr-only">done</span>}
        {!open && onReopen && <Button onClick={onReopen}>{reopenLabel ?? "Open"}</Button>}
      </div>
      {open && <div className="grid gap-3 border-t border-[var(--border)] px-4 py-3">{children}</div>}
    </li>
  );
}
