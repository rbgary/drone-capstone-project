"use client";

import { useState } from "react";
import { TimeSeries, type Band, type Series } from "@/components/ui/time-series";
import { unitSymbol, type Flag, type Track } from "@/lib/pipeline";

/**
 * One signal of one flight, raw vs clean (the owner, 2026-10-09: "the dirty
 * data versus the clean data").
 *
 *   Clean   what the pipeline analysed — the cleaner's flagged values left out,
 *           pressure corrected for height — against what was EXPECTED (the
 *           drone's cooling curve; the pressure's drift). The stretches that
 *           departed are shaded and named under the chart.
 *   Raw     every value as recorded, the flagged ones marked as points.
 *
 * A click on the chart once showed the camera frame nearest that moment
 * (2026-10-11); it went with the camera (2026-11-15).
 *
 * One y-axis, one unit, as every chart here (components/ui/time-series.tsx).
 */

export type RawReading = { index: number; t: number; value: number | null };

const VIEWS = [
  { key: "clean", label: "Clean" },
  { key: "raw", label: "Raw" },
] as const;

export function SignalPanel({
  title,
  track,
  raw,
  flags,
  bands,
  over = "flight",
}: {
  /** What the clock counts from: "flight", or "session" for a session's own. */
  over?: string;
  title: string;
  track: Track | null;
  /** The track's column as recorded, every reading, on the flight's clock. */
  raw: RawReading[];
  /** The cleaner's flags on this column (or the whole row). */
  flags: Flag[];
  bands: Band[];
}) {
  const [view, setView] = useState<"clean" | "raw">(track ? "clean" : "raw");
  const unit = track ? unitSymbol(track.unit) : "";
  const flagged = new Set(flags.map((f) => f.index));
  const time = new Map(raw.map((r) => [r.index, r.t]));

  const rawRows = raw.map((r) => ({
    t: r.t,
    recorded: r.value,
    flagged: flagged.has(r.index) ? r.value : null,
  }));
  const cleanRows = track
    ? track.indexes.flatMap((index, i) => {
        const t = time.get(index);
        return t === undefined ? [] : [{ t, observed: track.observed[i], expected: track.expected[i] }];
      })
    : [];
  const pressure = track?.signal === "pressure";
  const corrected = pressure && !track?.model.includes("height not measured");

  const cleanSeries: Series[] = [
    { key: "observed", label: corrected ? "Corrected for height" : "Measured", slot: 1, unit },
    { key: "expected", label: "Expected", slot: 2, unit },
  ];
  const rawSeries: Series[] = [
    { key: "recorded", label: "As recorded", slot: 1, unit },
    { key: "flagged", label: "Flagged by the cleaner", slot: 2, unit, points: true },
  ];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h4 className="text-base font-semibold">{title}{unit ? ` (${unit})` : ""}</h4>
        <div role="group" aria-label={`${title}: which data`}
             className="inline-flex rounded-lg border border-[var(--border)]">
          {VIEWS.map((v) => (
            <button key={v.key} type="button" onClick={() => setView(v.key)}
                    aria-pressed={view === v.key}
                    disabled={v.key === "clean" && !track}
                    className={`inline-flex min-h-11 items-center px-4 text-sm font-medium first:rounded-l-lg last:rounded-r-lg disabled:opacity-40 ${
                      view === v.key ? "bg-[var(--primary)] text-[var(--on-primary)]" : "hover:bg-[var(--surface-2)]"}`}>
              {v.label}
            </button>
          ))}
        </div>
      </div>
      <p className="text-sm text-[var(--muted)]">
        {view === "clean"
          ? track
            ? `What the pipeline analysed, against what was expected (${track.model}). ${
                track.noise !== null ? `Reading-to-reading noise ±${track.noise.toFixed(3)} ${unit}.` : ""}`
            : "Not analysed."
          : `Every value as recorded; ${flagged.size.toLocaleString()} flagged by the cleaner and left out of the analysis.`}
      </p>
      {view === "clean" ? (
        <TimeSeries rows={cleanRows} series={cleanSeries} xKey="t" xLabel={`Time in ${over} (s)`}
                    xFormat="seconds" yLabel={unit} digits={pressure ? 3 : 2} bands={bands} />
      ) : (
        <TimeSeries rows={rawRows} series={rawSeries} xKey="t" xLabel={`Time in ${over} (s)`}
                    xFormat="seconds" yLabel={unit} digits={pressure ? 3 : 2} bands={bands} />
      )}
    </div>
  );
}
