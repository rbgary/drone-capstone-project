/**
 * The data pipeline on the Control page: the DPP switch, and the result of
 * the last flight.
 *
 * THE SWITCH sits at the top of the page, in Auto and in Manual alike (the
 * owner, 2026-10-09), and is ON by default in both. It can be turned off
 * before a session starts or during one; the choice lasts until the session
 * ends (backend/agent/dronedeck/processing.py). It is read as a flight
 * BEGINS, so flipping it mid-flight is for the next one, and the switch says
 * so. The agent keeps the choice; this page only shows it
 * (Session.processing) and asks to change it.
 *
 * THE RESULT has the four states of every async surface here, plus the two
 * the pipeline adds: being processed (queued, running), failed with the
 * pipeline's own words and a way to run it again, not processed (DPP was off)
 * with a way to process it now, and the verdicts — one per inspection point,
 * or one for a Manual flight ("flight") — then the FINDINGS: each stretch of
 * the flight whose temperature or pressure departed from what was expected,
 * worst first, in the pipeline's own words (features/pipeline/
 * data-pipeline.txt). A version 1 result (before 2026-10-09) has none, and
 * one from before the classifier existed reads "no classifier yet" — the panel
 * says so.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { announceFindings } from "@/lib/os-notify";
import type { Run } from "@/App";
import { api, AgentError, type FlightResult, type PipelineFinding, type ProcessingJob, type Session } from "@/lib/agent";
import { formatTime } from "@/lib/format";
import { Button, Message, Panel, Spinner, StatusDot, Switch, type Tone } from "@/components/ui";

export function ProcessingSwitch({ session, run }: { session: Session; run: Run }) {
  const on = session.processing?.on ?? true;
  const chosen = session.processing?.chosen ?? false;
  const signedOut = session.state === "signed_out";
  const flying = session.activity === "mission" || session.activity === "manual" || session.activity === "program";
  const inSession = session.session_id !== null;
  const note = signedOut
    ? "Sign in to change it."
    : flying
      ? "Changes apply to the next flight."
      : chosen
        ? inSession ? "Your choice until this session ends." : "Your choice for the session you start next."
        : "On by default.";
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <Switch
        on={on}
        label="Process flights (DPP)"
        disabled={signedOut}
        title={signedOut ? "Sign in first." : undefined}
        onClick={() => void run(() => api.setProcessing(!on), on ? "Turn data processing off" : "Turn data processing on")}
      />
      <span className="text-xs text-[var(--muted)]">{note}</span>
    </div>
  );
}

/**
 * The auto-capture switch, beside the DPP's, in both modes (2026-10-24). On
 * by default; off keeps the camera live and saves only what is captured by
 * hand (agent camera/recording.py). Unlike the DPP it applies at once — the
 * recording reads it on every frame.
 */
export function CaptureSwitch({ session, run }: { session: Session; run: Run }) {
  const auto = session.capture?.auto ?? true;
  const chosen = session.capture?.chosen ?? false;
  const signedOut = session.state === "signed_out";
  const inSession = session.session_id !== null;
  const note = signedOut
    ? "Sign in to change it."
    : !auto
      ? "The camera stays on; only frames you capture by hand are saved."
      : chosen
        ? inSession ? "Your choice until this session ends." : "Your choice for the session you start next."
        : "On by default.";
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <Switch
        on={auto}
        label="Auto-capture"
        disabled={signedOut}
        title={signedOut ? "Sign in first." : undefined}
        onClick={() => void run(() => api.setCaptureAuto(!auto), auto ? "Turn auto-capture off" : "Turn auto-capture on")}
      />
      <span className="text-xs text-[var(--muted)]">{note}</span>
    </div>
  );
}

/** Which flight to show: the session's most recent flight to land, with its
 *  job if it has one; else the most recent job (a flight processed by hand). */
function lastFlight(session: Session): { id: string; job: ProcessingJob | null } | null {
  // Flight jobs only: a session's own job and a point judged in flight are not
  // a flight's result.
  const jobs = (session.processing?.jobs ?? []).filter((j) => (j.kind ?? "flight") === "flight");
  const landed = session.processing?.last_flight_id ?? null;
  if (landed) return { id: landed, job: jobs.find((j) => j.flight_id === landed) ?? null };
  return jobs[0] ? { id: jobs[0].flight_id, job: jobs[0] } : null;
}

const SEVERITY: Record<PipelineFinding["severity"], { tone: Tone; text: string; rank: number }> = {
  critical: { tone: "critical", text: "Critical", rank: 2 },
  warning: { tone: "warning", text: "Warning", rank: 1 },
  info: { tone: "idle", text: "Slight", rank: 0 },
};

const VERDICT: Record<string, { tone: Tone; text: string }> = {
  normal: { tone: "good", text: "Normal" },
  anomaly: { tone: "critical", text: "Anomaly" },
  insufficient_data: { tone: "idle", text: "Insufficient data" },
};

export function FlightResults({ session, run, flightId }: { session: Session; run: Run; flightId?: string }) {
  const found = flightId ? { id: flightId, job: session.processing?.jobs?.find((j) => j.flight_id === flightId && (j.kind ?? "flight") === "flight") ?? null } : lastFlight(session);
  const [result, setResult] = useState<FlightResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const id = found?.id ?? null;
  const state = found?.job?.state ?? null;
  // A job seen queued or running, then done, finished while the app was open:
  // its findings are announced to the OS once (lib/os-notify.ts).
  const seenRunning = useRef<string | null>(null);
  const announce = useRef<string | null>(null);
  useEffect(() => {
    if (id && (state === "queued" || state === "running")) seenRunning.current = id;
    if (id && state === "done" && seenRunning.current === id) {
      announce.current = id;
      seenRunning.current = null;
    }
  }, [id, state]);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    setError(null);
    try {
      const loaded = await api.flightResult(id);
      setResult(loaded);
      if (announce.current === id) {
        announce.current = null;
        void announceFindings(id, loaded.result.findings ?? []);
      }
    } catch (e) {
      setResult(null);
      setError(e instanceof AgentError ? e.message : "The result could not be read.");
    } finally {
      setLoading(false);
    }
  }, [id]);

  // Read the result when the flight changes and whenever its job finishes.
  useEffect(() => { if (id && (state === "done" || state === null)) void load(); }, [id, state, load]);

  if (!found) {
    return (
      <p className="text-xs text-[var(--muted)]">
        No flight yet this session. With processing on, each flight is processed when it lands and its verdicts appear here.
      </p>
    );
  }

  const processNow = (
    <Button onClick={() => void run(() => api.processFlight(found.id), "Process this flight")}>
      {state === "failed" || result ? "Process again" : "Process this flight"}
    </Button>
  );

  if (state === "queued" || state === "running") {
    return <Spinner label={state === "queued" ? "Waiting to be processed…" : "Processing this flight…"} />;
  }
  if (state === "failed") {
    return (
      <div className="grid gap-2">
        <Message tone="critical" text={`Processing failed: ${found.job?.error ?? "no reason was given."}`} />
        <div>{processNow}</div>
      </div>
    );
  }
  if (loading && !result) return <Spinner label="Reading the result…" />;
  if (!result) {
    return (
      <div className="grid gap-2">
        <p className="text-xs">{error ?? "This flight has not been processed."}</p>
        <div className="flex flex-wrap gap-2">
          {processNow}
          <Button onClick={() => void load()}>Check again</Button>
        </div>
      </div>
    );
  }

  const r = result.result;
  const placeholder = r.points.every((p) => p.verdict === "insufficient_data"
    && p.reasons.some((why) => why.includes("no classifier yet")));
  return (
    <div className="grid gap-2">
      <p className="mono text-xs text-[var(--muted)]">
        Flight {r.flight_id.slice(0, 8)} · processed {formatTime(r.created_at)} · pipeline {r.pipeline_version}
      </p>
      {placeholder && (
        <Message tone="idle" text="The pipeline ran, but its classify stage is not built yet (the team's work), so every point reads “insufficient data”. The readings below are real." />
      )}
      <ul className="grid gap-1.5">
        {r.points.map((p) => {
          const v = VERDICT[p.verdict] ?? { tone: "idle" as Tone, text: p.verdict };
          const counts = r.summary[p.point_id];
          return (
            <li key={p.point_id} className="grid gap-0.5 border-l-2 border-[var(--border)] pl-2 text-xs">
              <span className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="mono font-semibold">{p.point_id === "flight" ? "The whole flight" : p.point_id}</span>
                <StatusDot tone={v.tone}>{v.text}</StatusDot>
              </span>
              <span className="text-[var(--muted)]">
                {counts?.readings ?? 0} readings{p.reasons.length ? ` — ${p.reasons.join("; ")}` : ""}
              </span>
            </li>
          );
        })}
      </ul>
      {r.findings && (
        <div className="grid gap-1.5 border-t border-[var(--border)] pt-2">
          <p className="text-xs font-semibold">
            {r.findings.length === 0 ? "No findings — nothing departed from what was expected." : `Findings (${r.findings.length})`}
          </p>
          {r.findings.length > 0 && (
            <ul className="grid gap-1.5">
              {[...r.findings].sort((a, b) => SEVERITY[b.severity].rank - SEVERITY[a.severity].rank || a.t_start_s - b.t_start_s).map((f) => (
                <li key={f.id} className="grid gap-0.5 border-l-2 border-[var(--border)] pl-2 text-xs">
                  <span className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="font-semibold">{f.title}</span>
                    <StatusDot tone={SEVERITY[f.severity].tone}>{SEVERITY[f.severity].text}</StatusDot>
                  </span>
                  <span>{f.sentence}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {r.failures.length > 0 && (
        <ul className="grid gap-1">
          {r.failures.map((f, i) => (
            <li key={i} className="text-xs"><StatusDot tone="warning">{`${f.point_id} · ${f.stage}: ${f.reason}`}</StatusDot></li>
          ))}
        </ul>
      )}
      <div>{processNow}</div>
    </div>
  );
}

/** The switch and the last flight's result, as one panel. */
export function DataPipelinePanel({ session, run }: { session: Session; run: Run }) {
  return (
    <Panel title="Data processing" note="Turns a flight's readings into a verdict per inspection point, once the flight has landed. The switch is at the top of the page." bodyClassName="grid gap-3 px-4 py-3">
      <FlightResults session={session} run={run} />
    </Panel>
  );
}
