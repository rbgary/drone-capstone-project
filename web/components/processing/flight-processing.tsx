import type { FindingRow, PipelineResultRow } from "@/lib/queries";

/** The result fields this reads — a flight's, or a session's own (same shape). */
type Processed = Pick<PipelineResultRow, "tracks" | "flags" | "points" | "stages"
  | "failures" | "created_at" | "pipeline_version">;
/** A reading as the charts need it: a flight's row, or a session sample with
 *  its seq as the index. */
type Reading = { index: number; recorded_at: string; raw_temp: number | null;
                 station_pressure_hpa: number | null };
import type { Band } from "@/components/ui/time-series";
import { StatusBadge } from "@/components/ui/states";
import { LocalTime } from "@/components/ui/local-time";
import { SignalPanel } from "@/components/processing/signal-panel";
import { elapsedSeconds } from "@/lib/flight-format";
import {
  SEVERITY,
  SHADE,
  groupColor,
  readFlags,
  readPoints,
  readTracks,
  severityOf,
  type Flag,
} from "@/lib/pipeline";

/**
 * One flight's data processing: what ran, a verdict per inspection point, what
 * the cleaner took out, and the two signals raw vs clean against what was
 * expected — the same on the flight page and, once
 * per flight, on its session's page.
 */

const VERDICT = {
  normal: { status: "good", label: "Normal" },
  anomaly: { status: "critical", label: "Anomaly" },
  insufficient_data: { status: "warning", label: "Insufficient data" },
} as const;

function countBy<T>(items: T[], key: (item: T) => string): [string, number][] {
  const out = new Map<string, number>();
  for (const item of items) out.set(key(item), (out.get(key(item)) ?? 0) + 1);
  return [...out.entries()].sort((a, b) => b[1] - a[1]);
}

export function FlightProcessing({
  result,
  telemetry,
  findings,
  over = "flight",
}: {
  /** "session" for a session's own result: its charts count session time. */
  over?: string;
  result: Processed;
  telemetry: Reading[];
  findings: FindingRow[];
}) {
  const tracks = readTracks(result.tracks);
  const flags = readFlags(result.flags);
  const points = readPoints(result.points);
  const stages = Object.entries((result.stages ?? {}) as Record<string, string>);
  const failures = Array.isArray(result.failures) ? result.failures : [];
  const first = telemetry[0]?.recorded_at;
  const t = (iso: string) => (first ? Math.round(elapsedSeconds(iso, first) * 10) / 10 : 0);

  const bandsFor = (signal: string): Band[] =>
    findings.filter((f) => f.signal === signal).map((f) => ({
      x1: f.t_start_s, x2: f.t_end_s,
      color: groupColor(f.signal === "pressure" ? "pressure" : "temperature"),
      depth: SHADE[severityOf(f.severity)] / 100,
      label: `${SEVERITY[severityOf(f.severity)].label} — ${f.title}`,
    }));
  const flagsOn = (column: string): Flag[] =>
    flags.filter((f) => f.column === column || f.column === null);
  const raw = (column: "raw_temp" | "station_pressure_hpa") =>
    telemetry.map((r) => ({ index: r.index, t: t(r.recorded_at), value: r[column] }));

  // Position and battery are judged too, but a position "not measured" is a
  // fact about the base station, not a faulty sensor — said apart.
  const POSITION = new Set(["x_m", "y_m", "z_m"]);
  const sensorFlags = flags.filter((f) => f.column === null || !POSITION.has(f.column));
  const rowsOf = (kind: string) =>
    new Set(flags.filter((f) => f.column !== null && POSITION.has(f.column) && f.kind === kind)
      .map((f) => f.index)).size;
  const unmeasured = rowsOf("untrusted");
  const jumps = rowsOf("implausible");

  return (
    <div className="space-y-6">
      <p className="text-sm text-[var(--muted)]">
        Processed <LocalTime iso={result.created_at} /> · pipeline {result.pipeline_version}
        {stages.length > 0 && <> · {stages.map(([stage, name]) => `${stage} ${name}`).join(" · ")}</>}
      </p>

      {points.length > 0 && (
        <div className="overflow-x-auto [contain:paint] rounded-lg border border-[var(--border)]">
          <table className="w-full border-collapse text-left text-sm">
            <caption className="sr-only">A verdict for each inspection point</caption>
            <thead className="bg-[var(--surface-2)]">
              <tr>
                <th scope="col" className="px-4 py-3 font-medium">Point</th>
                <th scope="col" className="px-4 py-3 font-medium">Verdict</th>
                <th scope="col" className="px-4 py-3 font-medium">Why</th>
              </tr>
            </thead>
            <tbody>
              {points.map((p) => {
                const v = VERDICT[p.verdict];
                return (
                  <tr key={p.point_id} className="border-t border-[var(--border)] align-top">
                    <th scope="row" className="px-4 py-3 font-medium">
                      {p.point_id === "flight" ? "The whole flight" : p.point_id}
                    </th>
                    <td className="px-4 py-3"><StatusBadge status={v.status} label={v.label} /></td>
                    <td className="px-4 py-3">{p.reasons.join(" ")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
        <dt className="text-[var(--muted)]">Cleaned</dt>
        <dd>
          {sensorFlags.length === 0 ? "No sensor value flagged." : `${sensorFlags.length.toLocaleString()} sensor values flagged and left out: ${
            countBy(sensorFlags, (f) => `${f.column ?? "lost time before row"} ${f.kind}`).slice(0, 6)
              .map(([what, n]) => `${what} ${n}`).join(", ")}.`}
        </dd>
        {(unmeasured > 0 || jumps > 0) && (
          <>
            <dt className="text-[var(--muted)]">Position</dt>
            <dd>
              {unmeasured > 0 && `Not measured on ${unmeasured.toLocaleString()} readings — no base station was received. `}
              {jumps > 0 && `The estimate jumped impossibly fast on ${jumps.toLocaleString()} readings.`}
            </dd>
          </>
        )}
        {failures.length > 0 && (
          <>
            <dt className="text-[var(--muted)]">Failed</dt>
            <dd>
              {failures.map((f, i) => {
                const fail = f as { stage?: string; reason?: string };
                return <span key={i} className="block">{fail.stage}: {fail.reason}</span>;
              })}
            </dd>
          </>
        )}
      </dl>

      <div className="grid gap-10 xl:grid-cols-2">
        <SignalPanel title="Temperature" track={tracks.find((x) => x.signal === "temperature") ?? null}
                     raw={raw("raw_temp")} flags={flagsOn("raw_temp")} bands={bandsFor("temperature")} over={over} />
        <SignalPanel title="Pressure" track={tracks.find((x) => x.signal === "pressure") ?? null}
                     raw={raw("station_pressure_hpa")} flags={flagsOn("station_pressure_hpa")}
                     bands={bandsFor("pressure")} over={over} />
      </div>
    </div>
  );
}
