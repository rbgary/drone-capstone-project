/**
 * The data pipeline's result, as the web reads it from public.pipeline_results
 * and public.pipeline_findings (migration 20261009000016) — the shapes the
 * agent's result.json has (backend/agent/dronedeck/pipeline/contracts.py).
 *
 * The JSON columns come back as `Json`; every reader here checks the shape it
 * needs and drops what does not fit, so a result from an older pipeline
 * version shows what it has instead of breaking the page.
 */

import type { Json } from "@/types/database";

export type Severity = "info" | "warning" | "critical";

/** One analysed signal: what was measured (after any correction) and what
 *  was expected, reading by reading. */
export type Track = {
  signal: "temperature" | "pressure";
  column: string;
  unit: string;
  model: string;
  noise: number | null;
  indexes: number[];
  observed: number[];
  expected: number[];
};

/** [reading index, column (null = whole row), kind, reason] */
export type Flag = { index: number; column: string | null; kind: string; reason: string };

export type PointVerdict = {
  point_id: string;
  verdict: "normal" | "anomaly" | "insufficient_data";
  reasons: string[];
};

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
const isNumbers = (v: unknown): v is number[] =>
  Array.isArray(v) && v.every((x) => typeof x === "number");

export function readTracks(value: Json): Track[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((t) => {
    if (!isObject(t)) return [];
    const { signal, column, unit, model, noise, indexes, observed, expected } = t;
    if ((signal !== "temperature" && signal !== "pressure") || typeof column !== "string"
        || typeof unit !== "string" || typeof model !== "string"
        || !isNumbers(indexes) || !isNumbers(observed) || !isNumbers(expected)) return [];
    return [{ signal, column, unit, model, noise: typeof noise === "number" ? noise : null,
              indexes, observed, expected }];
  });
}

export function readFlags(value: Json): Flag[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((f) =>
    Array.isArray(f) && typeof f[0] === "number" && typeof f[2] === "string"
      ? [{ index: f[0], column: typeof f[1] === "string" ? f[1] : null, kind: f[2],
           reason: typeof f[3] === "string" ? f[3] : "" }]
      : []);
}

export function readPoints(value: Json): PointVerdict[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((p) => {
    if (!isObject(p) || typeof p.point_id !== "string") return [];
    const verdict = p.verdict === "normal" || p.verdict === "anomaly" ? p.verdict
      : "insufficient_data";
    const reasons = Array.isArray(p.reasons) ? p.reasons.filter((r): r is string =>
      typeof r === "string") : [];
    return [{ point_id: p.point_id, verdict, reasons }];
  });
}

export const SEVERITY: Record<Severity, { status: "critical" | "serious" | "warning";
                                          label: string; rank: number }> = {
  critical: { status: "critical", label: "Critical", rank: 2 },
  warning: { status: "serious", label: "Warning", rank: 1 },
  info: { status: "warning", label: "Slight", rank: 0 },
};

export function severityOf(value: string): Severity {
  return value === "critical" || value === "warning" ? value : "info";
}

/** "°C", "°F" or the unit as written. */
export function unitSymbol(unit: string): string {
  return unit === "C" ? "°C" : unit === "F" ? "°F" : unit;
}

/** m:ss from a number of seconds. */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/**
 * WHAT THE PAGES HIGHLIGHT — "marks" (the owner, 2026-10-09: "a different
 * colour for each data group; the severity is the shade — lighter is less
 * severe, deeper is more").
 *
 *   anomaly  a finding: a stretch whose temperature (orange) or pressure
 *            (blue) departed from what was expected
 *   fault    the cleaner's flags, merged into spans: a sensor value that was
 *            missing, stuck, a spike or out of range (purple); a position the
 *            drone did not measure, or a battery reading no load explains
 *            (teal); time lost between readings (purple, on the time cells)
 *
 * The hue is the data group (tokens --group-*, app/globals.css); the depth of
 * the tint is the severity (SHADE). Never colour alone: every mark has a
 * shape and a word beside it, and its meaning on hover, focus or tap.
 */

export type Group = "temperature" | "pressure" | "fault" | "position";

export const GROUPS: Record<Group, { label: string }> = {
  temperature: { label: "Temperature" },
  pressure: { label: "Pressure" },
  fault: { label: "Sensor fault" },
  position: { label: "Position & battery" },
};

/** Tint strength per severity, % of the group colour: lighter = less severe. */
export const SHADE: Record<Severity, number> = { info: 14, warning: 28, critical: 48 };

export function groupColor(group: Group): string {
  return `var(--group-${group})`;
}

/** The cell tint for a group at a severity. */
export function groupTint(group: Group, severity: Severity): string {
  return `color-mix(in srgb, ${groupColor(group)} ${SHADE[severity]}%, transparent)`;
}

export type Mark = {
  id: string;
  kind: "anomaly" | "fault";
  group: Group;
  severity: Severity;
  /** Reading index (telemetry `index`, or a session sample's `seq`), inclusive. */
  start: number;
  end: number;
  /** Seconds into the flight (or session); null when not known. */
  tStart: number | null;
  tEnd: number | null;
  /** The table columns it tints. */
  columns: string[];
  title: string;
  body: string;
};

type FindingLike = {
  id: string; signal: string; severity: string; start_index: number; end_index: number;
  t_start_s: number; t_end_s: number; title: string; sentence: string;
};

/** The columns each signal's anomaly tints. */
const ANOMALY_COLUMNS: Record<string, string[]> = {
  temperature: ["raw_temp", "corrected_temp"],
  pressure: ["station_pressure_hpa"],
};

export function anomalyMarks(findings: readonly FindingLike[]): Mark[] {
  return findings.map((f) => ({
    id: f.id, kind: "anomaly" as const,
    group: f.signal === "pressure" ? "pressure" as const : "temperature" as const,
    severity: severityOf(f.severity), start: f.start_index, end: f.end_index,
    tStart: f.t_start_s, tEnd: f.t_end_s,
    columns: ANOMALY_COLUMNS[f.signal] ?? [], title: f.title, body: f.sentence,
  })).sort((a, b) => a.start - b.start);
}

/** Which family a flagged column belongs to, and the words for it. */
const FAMILY: Record<string, { family: string; group: Group; words: string; columns: string[] }> = {
  raw_temp: { family: "temperature", group: "fault", words: "temperature",
              columns: ["raw_temp", "corrected_temp"] },
  corrected_temp: { family: "temperature", group: "fault", words: "temperature",
                    columns: ["raw_temp", "corrected_temp"] },
  station_pressure_hpa: { family: "pressure", group: "fault", words: "pressure",
                          columns: ["station_pressure_hpa"] },
  x_m: { family: "position", group: "position", words: "position", columns: ["x_m", "y_m", "z_m"] },
  y_m: { family: "position", group: "position", words: "position", columns: ["x_m", "y_m", "z_m"] },
  z_m: { family: "position", group: "position", words: "position", columns: ["x_m", "y_m", "z_m"] },
  battery_v: { family: "battery", group: "position", words: "battery", columns: ["battery_v"] },
};
const TIME_FAMILY = { family: "time", group: "fault" as Group, words: "readings",
                      columns: ["index", "seq", "recorded_at"] };

/** How severe each kind of flag is: a value that should not be trusted at all
 *  is a warning; one missing or briefly off, slight. */
const KIND: Record<string, { severity: Severity; title: (words: string) => string }> = {
  missing: { severity: "info", title: (w) => `No ${w} recorded` },
  spike: { severity: "info", title: (w) => `A ${w} spike` },
  gap: { severity: "info", title: () => "Time lost between readings" },
  untrusted: { severity: "info", title: () => "Position not measured (no base station)" },
  stuck: { severity: "warning", title: (w) => `The ${w} sensor stuck` },
  out_of_range: { severity: "warning", title: (w) => `A ${w} reading out of range` },
  implausible: { severity: "warning", title: (w) => `An impossible ${w} jump` },
};

/**
 * The cleaner's flags as marks: consecutive readings with the same kind of
 * fault in the same family of columns are ONE span. `timeOf` gives a reading's
 * seconds into the flight (or session), when known.
 */
export function faultMarks(flags: readonly Flag[],
                           timeOf: (index: number) => number | null = () => null): Mark[] {
  const sorted = [...flags].sort((a, b) => a.index - b.index);
  const open = new Map<string, Mark & { reasons: string[]; count: number }>();
  const out: Mark[] = [];
  const close = (key: string) => {
    const m = open.get(key);
    if (!m) return;
    open.delete(key);
    const { reasons, count, ...mark } = m;
    // The cleaner's sentence, without the column name it starts with ("x_m: …")
    // — the mark already says what it is about; how many readings, the
    // tables and the tooltip say beside it.
    void count;
    out.push({ ...mark, body: reasons[0].replace(/^[a-z_]+: /, "").replace(/^./, (c) => c.toUpperCase()) });
  };
  for (const f of sorted) {
    const fam = f.column === null ? TIME_FAMILY : FAMILY[f.column];
    if (!fam) continue;                       // a column no table shows
    const kind = KIND[f.kind] ?? { severity: "info" as Severity, title: (w: string) => `A ${w} fault` };
    const key = `${fam.family}:${f.kind}`;
    const m = open.get(key);
    if (m && f.index <= m.end + 1) {
      if (f.index > m.end) { m.end = f.index; m.tEnd = timeOf(f.index); m.count += 1; }
      continue;
    }
    close(key);
    open.set(key, {
      id: `${key}:${f.index}`, kind: "fault", group: fam.group, severity: kind.severity,
      start: f.index, end: f.index, tStart: timeOf(f.index), tEnd: timeOf(f.index),
      columns: fam.columns, title: kind.title(fam.words), body: "", reasons: [f.reason], count: 1,
    });
  }
  for (const key of [...open.keys()]) close(key);
  return out.sort((a, b) => a.start - b.start || a.id.localeCompare(b.id));
}

/** A reading's seconds since the first row, by its index (or seq). */
export function timesOf(rows: readonly { [key: string]: unknown }[],
                        indexKey = "index"): (index: number) => number | null {
  const first = rows.find((r) => typeof r.recorded_at === "string")?.recorded_at;
  const t0 = typeof first === "string" ? Date.parse(first) : Number.NaN;
  const at = new Map<number, number>();
  for (const r of rows) {
    const i = r[indexKey];
    if (typeof i === "number" && typeof r.recorded_at === "string") {
      at.set(i, (Date.parse(r.recorded_at) - t0) / 1000);
    }
  }
  return (index) => at.get(index) ?? null;
}

/** Everything a table marks for one flight or session: its anomalies and the
 *  cleaner's flags as spans. */
export function marksOf(findings: readonly FindingLike[], flags: readonly Flag[],
                        rows: readonly { [key: string]: unknown }[], indexKey = "index"): Mark[] {
  return [...anomalyMarks(findings), ...faultMarks(flags, timesOf(rows, indexKey))]
    .sort((a, b) => a.start - b.start);
}

/** Every mark covering a reading. */
export function marksAt(marks: readonly Mark[], index: number): Mark[] {
  return marks.filter((m) => index >= m.start && index <= m.end);
}

/** The worst mark of a list (by severity, then anomaly over fault). */
export function worstMark(marks: readonly Mark[]): Mark | undefined {
  return [...marks].sort((a, b) => SEVERITY[b.severity].rank - SEVERITY[a.severity].rank
    || (a.kind === b.kind ? 0 : a.kind === "anomaly" ? -1 : 1))[0];
}

/** One reading of an anomalous stretch: the stored value, and what the
 *  pipeline measured (after any correction) against what it expected. */
export type ExcerptRow = {
  index: number;
  t: number;
  raw: number | null;
  measured: number | null;
  expected: number | null;
};

/**
 * The readings of a stretch, at most `max` of them: the first, the last, the
 * one furthest from what was expected, and evenly between — so a long
 * stretch reads in a glance without hiding where it peaked.
 */
export function excerptOf(
  rows: readonly { index: number; recorded_at: string; [key: string]: unknown }[],
  track: Track | null,
  tStart: number,
  rawColumn: string,
  max = 10,
): { rows: ExcerptRow[]; total: number } {
  if (rows.length === 0) return { rows: [], total: 0 };
  const at = new Map<number, number>();
  track?.indexes.forEach((index, i) => at.set(index, i));
  const first = Date.parse(rows[0].recorded_at);
  const all: ExcerptRow[] = rows.map((r) => {
    const i = at.get(r.index);
    const raw = r[rawColumn];
    return {
      index: r.index,
      t: tStart + (Date.parse(r.recorded_at) - first) / 1000,
      raw: typeof raw === "number" ? raw : null,
      measured: i !== undefined && track ? track.observed[i] : null,
      expected: i !== undefined && track ? track.expected[i] : null,
    };
  });
  if (all.length <= max) return { rows: all, total: all.length };
  const gap = (r: ExcerptRow) =>
    r.measured !== null && r.expected !== null ? Math.abs(r.measured - r.expected) : -1;
  const peak = all.reduce((best, r, i) => (gap(r) > gap(all[best]) ? i : best), 0);
  const keep = new Set([0, all.length - 1, peak]);
  for (let k = 1; keep.size < max && k < max; k++) {
    keep.add(Math.round((k * (all.length - 1)) / (max - 1)));
  }
  return { rows: [...keep].sort((a, b) => a - b).map((i) => all[i]), total: all.length };
}

/** The stored column an anomaly's signal is read from, and what to call it. */
export function rawColumnOf(signal: string, track: Track | null): { column: string; label: string } {
  if (signal === "pressure") return { column: "station_pressure_hpa", label: "Stored pressure" };
  return { column: track?.column ?? "raw_temp", label: "Stored temp" };
}

/** Each finding's excerpt, from its flight's result and its stretch's readings. */
export function excerptsFor(
  findings: readonly { id: string; flight_id: string | null; session_id?: string | null;
                       signal: string; t_start_s: number }[],
  tracksOf: (flightId: string) => Track[],
  readings: Record<string, { index: number; recorded_at: string; [key: string]: unknown }[]>,
): Record<string, { rows: ExcerptRow[]; total: number; rawLabel: string }> {
  const out: Record<string, { rows: ExcerptRow[]; total: number; rawLabel: string }> = {};
  for (const f of findings) {
    const track = tracksOf(f.flight_id ?? f.session_id ?? "").find((t) => t.signal === f.signal)
      ?? null;
    const raw = rawColumnOf(f.signal, track);
    out[f.id] = { ...excerptOf(readings[f.id] ?? [], track, f.t_start_s, raw.column), rawLabel: raw.label };
  }
  return out;
}
