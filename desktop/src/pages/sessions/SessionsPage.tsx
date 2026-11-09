/** Every session this computer recorded, and one session in full. */

import { useCallback, useEffect, useState } from "react";
import { AgentError, api, sessionFrame, sessionFrameUpscaled, type FlightRecord, type Mode, type SessionRecord } from "@/lib/agent";
import { endReasonLabel, formatDateTime, formatDuration, formatNumber, formatTime } from "@/lib/format";
import { Button, Message, PageHeader, Panel, Spinner, Stat, StatusDot } from "@/components/ui";
import { CaptureGallery, type GalleryLoad } from "@/components/CaptureGallery";
import { WINDOWS, WINDOW_VARIABLES } from "@/pages/windows/SensorWindow";
import { flightTitle } from "./AllFlights";
import { FlightPlaybackView } from "./Playback";
import {
  DashboardLink, Disclosure, HiddenByMode, Load, LoadState, MODE_LABEL, ReadingsTable, SessionRow,
  useSessions,
} from "./parts";

export function SessionsPage({ refreshKey, mode, selectedId, onSelect }: {
  refreshKey: unknown;
  mode: Mode;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}) {
  const [allModes, setAllModes] = useState(false);
  const filter = allModes ? null : mode;
  const { state, hidden, reload } = useSessions(200, refreshKey, filter);

  if (selectedId) {
    return <SessionDetail id={selectedId} mode={filter} onBack={() => onSelect(null)} />;
  }

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <PageHeader eyebrow="Operate" title={allModes ? "Sessions" : `${MODE_LABEL[mode]} sessions`}>
          Every session run on this computer: who ran it, when it started and ended, and what the
          drone reported. Open one for its flights and readings. Everyone's flights, to watch
          again, are under Playback in the sidebar.
        </PageHeader>
        <DashboardLink>View all in the dashboard</DashboardLink>
      </div>

      <HiddenByMode mode={mode} hidden={hidden} onShowAll={() => setAllModes(true)} />

      <Panel title="All sessions" action={<Button onClick={reload}>Refresh</Button>}>
        <LoadState
          state={state}
          reload={reload}
          empty={state.kind === "ready" && state.data.length === 0}
          emptyText={<p>No sessions on this computer yet.</p>}
        >
          {(records) => (
            <ul className="-mx-5 divide-y divide-[var(--border)]">
              {records.map((r) => <SessionRow key={r.id} record={r} onOpen={() => onSelect(r.id)} />)}
            </ul>
          )}
        </LoadState>
      </Panel>
    </div>
  );
}

/** What a flight is called on this page — its program, or its mode. */
function flightName(f: FlightRecord): string {
  return flightTitle(f);
}

function SessionDetail({ id, mode, onBack }: { id: string; mode: Mode | null; onBack: () => void }) {
  const [state, setState] = useState<Load<SessionRecord>>({ kind: "loading" });
  // A flight being played back replaces the session's page; Back returns to it.
  const [playing, setPlaying] = useState<FlightRecord | null>(null);
  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      setState({ kind: "ready", data: await api.readSession(id) });
    } catch (e) {
      setState({ kind: "error", message: e instanceof AgentError ? e.message : "Could not load this session." });
    }
  }, [id]);
  useEffect(() => { void load(); }, [load]);

  if (playing) {
    return <FlightPlaybackView flightId={playing.id} title={flightName(playing)} onBack={() => setPlaying(null)} />;
  }

  return (
    <div className="grid gap-4">
      <div>
        <button type="button" onClick={onBack} className="text-sm font-medium text-[var(--muted)] hover:text-[var(--foreground)]">
          ← All sessions
        </button>
      </div>
      {state.kind === "loading" && <Spinner label="Loading the session…" />}
      {state.kind === "error" && (
        <div className="grid gap-3">
          <Message tone="critical" text={state.message} />
          <div><Button onClick={() => void load()}>Try again</Button></div>
        </div>
      )}
      {state.kind === "ready" && <SessionDetailBody record={state.data} mode={mode} onPlay={setPlaying} />}
    </div>
  );
}

function SessionDetailBody({ record, mode, onPlay }: {
  record: SessionRecord;
  mode: Mode | null;
  onPlay: (flight: FlightRecord) => void;
}) {
  const s = record.summary;
  return (
    <>
      <PageHeader title={`Session · ${formatDateTime(record.started_at)}`}>
        Run by {record.operator_name && record.operator_name !== record.operator_email
          ? `${record.operator_name} (${record.operator_email})`
          : record.operator_email}{" "}
        on drone {record.drone_hardware_id ?? "unknown"}.
      </PageHeader>

      {/* One row of eight rather than two of four: the whole summary of a
          session should be readable without the eye travelling down a column. */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 2xl:grid-cols-8">
        <Stat label="Started" value={formatTime(record.started_at)} />
        <Stat label="Ended" value={record.ended_at ? formatTime(record.ended_at) : "Running"}
              hint={endReasonLabel(record.end_reason)} />
        <Stat label="Duration" value={formatDuration(record.duration_s)} />
        <Stat label="Flights" value={String(record.flights.length)}
              hint={`${record.mode === "auto" ? "Auto" : "Manual"}${record.assisted ? "" : " · unassisted"}`} />
        <Stat label="Battery start" value={s.battery_start_v} unit="V" />
        <Stat label="Battery lowest" value={s.battery_min_v} unit="V" />
        <Stat label="Highest" value={s.max_height_m} unit="m" hint="Above the floor at takeoff" />
        <Stat label="Temperature" value={s.temp_min_c != null ? `${formatNumber(s.temp_min_c, 1)}–${formatNumber(s.temp_max_c, 1)}` : null}
              unit="°C" hint="Raw sensor range" />
      </section>

      <Panel title={mode ? `${MODE_LABEL[mode]} flights` : "Flights"}>
        {record.flights.filter((f) => !mode || f.mode === mode).length === 0 ? (
          <p className="text-sm text-[var(--muted)]">
            No {mode ? `${MODE_LABEL[mode].toLowerCase()} ` : ""}flights in this session — checks only.
          </p>
        ) : (
          <ul className="divide-y divide-[var(--border)]">
            {record.flights.filter((f) => !mode || f.mode === mode).map((f) => (
              <li key={f.id} className="flex flex-wrap items-center justify-between gap-3 py-3 text-sm">
                <span>
                  <span className="font-medium">{flightName(f)}</span>
                  <span className="block text-[var(--muted)]">
                    {formatTime(f.started_at)} – {formatTime(f.ended_at)}
                  </span>
                </span>
                <span className="flex flex-wrap items-center gap-3">
                  {f.abort_reason
                    ? <StatusDot tone="warning">Aborted: {f.abort_reason.replace(/_/g, " ")}</StatusDot>
                    : <StatusDot tone="good">{(f.outcome ?? "completed").replace(/_/g, " ")}</StatusDot>}
                  <Button onClick={() => onPlay(f)} title="Watch this flight again on the room map">Play back</Button>
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <ManualCaptures sessionId={record.id} />

      {WINDOWS.map((w, index) => (
        <Disclosure
          key={w.key}
          open={index === 0}
          summaryClassName="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-2.5"
          summary={
            <>
              <span className="mono text-xs font-bold uppercase tracking-[0.06em] text-[var(--heading)]">
                {w.label} readings
              </span>
              <span aria-hidden="true" className="text-[var(--muted)] transition-transform group-open:rotate-90">›</span>
            </>
          }
        >
          {() => <ReadingsTable sessionId={record.id} variables={WINDOW_VARIABLES[w.key]} mode={mode} />}
        </Disclosure>
      ))}
    </>
  );
}


/**
 * The session's frames captured by hand (the Capture page), from this
 * computer's files — labelled as manual captures of this session. Hidden
 * when it has none, so a session without any reads as it always did.
 */
function ManualCaptures({ sessionId }: { sessionId: string }) {
  const [load, setLoad] = useState<GalleryLoad>({ kind: "loading" });
  const read = useCallback(async () => {
    setLoad({ kind: "loading" });
    try {
      setLoad({ kind: "ready", frames: (await api.sessionFrames(sessionId, "manual")).frames });
    } catch (e) {
      setLoad({ kind: "error", message: e instanceof AgentError ? e.message : "The captures could not be read." });
    }
  }, [sessionId]);
  useEffect(() => { void read(); }, [read]);
  const frame = useCallback((seq: number) => sessionFrame(sessionId, seq), [sessionId]);
  const upscaled = useCallback((seq: number) => sessionFrameUpscaled(sessionId, seq), [sessionId]);

  if (load.kind === "ready" && load.frames.length === 0) return null;
  return (
    <Panel title={load.kind === "ready" ? `Captured manually · ${load.frames.length}` : "Captured manually"}
           note="Pictures taken by hand during this session, on the Capture page.">
      <CaptureGallery load={load} read={frame} readUpscaled={upscaled} onRetry={() => void read()}
                      emptyText="No manual captures." />
    </Panel>
  );
}
