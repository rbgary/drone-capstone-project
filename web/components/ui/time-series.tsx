"use client";

import { useId, useState } from "react";
import { formatLocal, useIsClient } from "@/components/ui/local-time";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  usePlotArea,
  useXAxisScale,
  useYAxisScale,
} from "recharts";

/**
 * The one time-series chart. Every other chart surface composes this.
 *
 * Three rules are enforced here rather than left to each caller, because each
 * one is a mistake that is easy to make four times:
 *
 * **One Y-axis, always.** There is deliberately no `yAxisId` seam. Two
 * measures of different scale are two charts — a dual axis lets the author
 * place a crossing wherever they like, and the reader cannot tell.
 *
 * **A table view ships with every chart.** The validated palette puts
 * light-mode aqua at 2.74:1 and yellow at 2.11:1 against the light surface,
 * under the 3:1 needed for a non-text mark. That is allowed only with relief,
 * so the relief is built in rather than remembered.
 *
 * **Colour is never the only channel.** Two or more series get a legend, and
 * up to four are also direct-labelled at the line's end.
 */

export type Series = {
  /** Row key holding this series' value. */
  key: string;
  label: string;
  /** Fixed slot, 1–4. Assigned by identity, never by rank or by sort order. */
  slot: 1 | 2 | 3 | 4;
  unit?: string;
  /**
   * `label` is an ISO timestamp, to be shown in the viewer's timezone. Formatting
   * a time into the label on the server would print the server's zone (UTC on
   * Vercel) — see `LocalTime`.
   */
  labelIsTime?: boolean;
  /** Points only, no line — values marked rather than traced (e.g. the
   *  readings the cleaner flagged). */
  points?: boolean;
};

/** A shaded stretch of x, named in words under the chart as well — the shade
 *  is never the only channel. Needs a numeric x (xFormat "seconds"). */
export type Band = {
  x1: number;
  x2: number;
  label: string;
  /** A colour token (the data group's: lib/pipeline groupColor). */
  color: string;
  /** How deep the fill, 0–1 — the severity's shade (lib/pipeline SHADE). */
  depth: number;
};

type Row = Record<string, number | string | null>;

const AXIS_TICK = { fill: "var(--muted)", fontSize: 11 };

/**
 * How to print the x value. A name rather than a function, because pages are
 * Server Components and a function cannot cross into a Client Component.
 */
export type XFormat = "seconds" | "clock" | "raw";

function formatX(value: unknown, format: XFormat): string {
  if (value === null || value === undefined) return "—";
  switch (format) {
    case "seconds":
      return typeof value === "number" ? `${value.toFixed(1)} s` : String(value);
    case "clock":
      return new Date(String(value)).toLocaleTimeString();
    case "raw":
      return String(value);
    default: {
      const unhandled: never = format;
      return unhandled;
    }
  }
}

/**
 * A y-scale that fits the data and ticks on round numbers.
 *
 * Fitting the data matters more than zero for lines: a battery moving 4.1 →
 * 3.7 V drawn on 0–8 V is flat. Rounding matters for reading: ticks at 74.55
 * and 73.5 make every value a subtraction.
 *
 * Computed here and handed to Recharts as explicit ticks. Given only a domain,
 * Recharts chooses its own ticks inside it — unevenly (0, 0.15, 0.3, 0.5) — and
 * given a domain *function* it drew no axis at all for some ranges.
 */
function niceScale(rows: Row[], series: Series[]): { domain: [number, number]; ticks: number[] } {
  let min = Infinity;
  let max = -Infinity;
  for (const row of rows) {
    for (const s of series) {
      const v = row[s.key];
      if (typeof v === "number" && Number.isFinite(v)) {
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
  }
  if (!Number.isFinite(min)) return { domain: [0, 1], ticks: [0, 0.5, 1] };

  const span = max - min || Math.abs(max) * 0.1 || 1; // a flat line still gets a band
  const raw = span / 5;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((c) => c >= raw) ?? raw;
  let lo = Math.floor(min / step) * step;
  let hi = Math.ceil(max / step) * step;
  if (lo === hi) {
    lo -= step;
    hi += step;
  }

  // Rounded to the step's precision, so 3.7000000000000002 prints as 3.7.
  const decimals = Math.max(0, -Math.floor(Math.log10(step)) + 1);
  const round = (n: number) => Number(n.toFixed(decimals));
  const ticks: number[] = [];
  for (let t = lo; t <= hi + step / 2; t += step) ticks.push(round(t));
  return { domain: [round(lo), round(hi)], ticks };
}

/** Minimum vertical gap between two end labels, px. One line of 11px text. */
const LABEL_GAP = 13;

/**
 * Where each end label goes, given where each line ends.
 *
 * Lines that finish at nearly the same value would print their names on top of
 * each other. Labels keep their order by height and are pushed apart until
 * they clear; the line itself is not moved, only its name.
 */
function spreadLabels(rawY: Map<string, number>): Map<string, number> {
  const sorted = [...rawY.entries()].sort((a, b) => a[1] - b[1]);
  const placed = new Map<string, number>();
  let previous = -Infinity;
  for (const [key, y] of sorted) {
    const next = Math.max(y, previous + LABEL_GAP);
    placed.set(key, next);
    previous = next;
  }
  return placed;
}

/**
 * Each line's name at its right-hand end.
 *
 * Positions come from the chart's own y scale, computed for every series at
 * once, so two lines that finish at the same value get labels pushed apart
 * instead of printed over each other. Rendering a label per line cannot do
 * that: each one only knows where its own line ends.
 */
function EndLabels({ rows, series, xKey }: { rows: Row[]; series: Series[]; xKey: string }) {
  const xScale = useXAxisScale();
  const yScale = useYAxisScale();
  const plot = usePlotArea();
  if (!xScale || !yScale || !plot) return null;

  const ends = new Map<string, number>();
  const endX = new Map<string, number>();
  const right = plot.x + plot.width;
  for (const s of series) {
    // The last reading this series actually has; a trailing null is a gap,
    // and a label floating where no line ends would misidentify another.
    const last = [...rows].reverse().find((r) => typeof r[s.key] === "number");
    if (!last) continue;
    const y = yScale(last[s.key] as number);
    const x = xScale(last[xKey] as number);
    if (typeof y === "number" && Number.isFinite(y)) ends.set(s.key, y);
    // Beside where the line stops, not at the chart edge: in a comparison a
    // short flight ends mid-chart and its name belongs there.
    endX.set(s.key, typeof x === "number" && Number.isFinite(x) ? Math.min(x, right) : right);
  }
  const placed = spreadLabels(ends);

  return (
    <g aria-hidden="true">
      {series.map((s) =>
        placed.has(s.key) ? (
          <text
            key={s.key}
            x={(endX.get(s.key) ?? right) + 6}
            y={(placed.get(s.key) as number) + 4}
            fontSize={11}
            // Text wears a text token; the line beside it carries identity.
            fill="var(--foreground)"
          >
            {s.label}
          </text>
        ) : null,
      )}
    </g>
  );
}

function formatValue(value: unknown, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return typeof value === "number" ? value.toFixed(digits) : String(value);
}

export function TimeSeries({
  rows,
  series: rawSeries,
  xKey,
  xLabel,
  yLabel,
  height = 280,
  digits = 2,
  xFormat = "raw",
  bands = [],
}: {
  rows: Row[];
  series: Series[];
  xKey: string;
  xLabel?: string;
  yLabel?: string;
  height?: number;
  digits?: number;
  xFormat?: XFormat;
  bands?: Band[];
}) {
  const client = useIsClient();
  const series = rawSeries.map((s) =>
    s.labelIsTime ? { ...s, label: client ? formatLocal(s.label, "short") : s.label } : s,
  );
  const [showTable, setShowTable] = useState(false);
  // Two to four series are named at the end of their line as well as in the
  // legend, so identity never rests on matching a colour swatch.
  const directLabels = series.length > 1 && series.length <= 4;
  const tableId = useId();
  const yAxis = niceScale(rows, series);
  // Wide enough for the longest tick ("1026.55") AND the rotated unit beside
  // it — a fixed width put "hPa" on top of the numbers.
  const yWidth = Math.max(52, Math.max(...yAxis.ticks.map((t) => String(t).length)) * 7 + 26);

  // Nothing to draw is a state, not an empty box with axes.
  if (rows.length === 0) {
    return (
      <p className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6 text-sm text-[var(--muted)]">
        No readings in this range.
      </p>
    );
  }

  return (
    <figure className="m-0">
      <div style={{ width: "100%", height }}>
        <ResponsiveContainer>
          <LineChart
            data={rows}
            margin={{ top: 8, right: directLabels ? 96 : 16, bottom: 16, left: 0 }}
          >
            {/* Furniture recedes: the data is the only thing at full strength. */}
            <CartesianGrid stroke="var(--grid)" vertical={false} />
            <XAxis
              dataKey={xKey}
              // Elapsed seconds are a continuous scale; spacing ticks by value
              // rather than one per sample keeps them readable.
              type={xFormat === "seconds" ? "number" : "category"}
              domain={xFormat === "seconds" ? ["dataMin", "dataMax"] : undefined}
              tickCount={8}
              minTickGap={24}
              interval="preserveStartEnd"
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={{ stroke: "var(--grid)" }}
              tickFormatter={(v) => formatX(v, xFormat)}
              label={
                xLabel
                  ? { value: xLabel, position: "insideBottom", offset: -2, fill: "var(--muted)", fontSize: 11 }
                  : undefined
              }
            />
            <YAxis
              // Fit the data, not zero — see niceScale.
              domain={yAxis.domain}
              ticks={yAxis.ticks}
              allowDataOverflow={false}
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={false}
              width={yWidth}
              label={
                yLabel
                  ? { value: yLabel, angle: -90, position: "insideLeft", fill: "var(--muted)", fontSize: 11 }
                  : undefined
              }
            />
            <Tooltip
              contentStyle={{
                background: "var(--surface)",
                border: "1px solid var(--border)",
                borderRadius: 8,
                fontSize: 12,
                // Text wears text tokens; the swatch beside it carries identity.
                color: "var(--foreground)",
              }}
              labelStyle={{ color: "var(--muted)" }}
              cursor={{ stroke: "var(--axis)", strokeWidth: 1 }}
              formatter={(value, name) => {
                const match = series.find((s) => s.label === name);
                return [
                  `${formatValue(value, digits)}${match?.unit ? ` ${match.unit}` : ""}`,
                  name,
                ];
              }}
              labelFormatter={(v) => formatX(v, xFormat)}
            />
            {series.length > 1 && (
              <Legend
                wrapperStyle={{ fontSize: 12, paddingTop: 12 }}
                iconType="plainline"
                // Keep the series' own order. The default sorts by name, which
                // puts a legend out of step with the slots the colours follow.
                itemSorter={null}
                // Recharts colours legend text with the series colour; light-mode
                // aqua is 2.74:1 as text. The swatch carries identity, the words
                // wear a text token.
                formatter={(value) => (
                  <span style={{ color: "var(--foreground)" }}>{value}</span>
                )}
              />
            )}
            {xFormat === "seconds" && bands.map((b, i) => (
              <ReferenceArea
                key={`band-${i}`}
                x1={b.x1}
                x2={b.x2}
                fill={b.color}
                fillOpacity={b.depth}
                stroke={b.color}
                strokeOpacity={0.7}
                ifOverflow="extendDomain"
              />
            ))}
            {series.map((s) => (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                name={s.label}
                stroke={s.points ? "none" : `var(--series-${s.slot})`}
                strokeWidth={s.points ? 0 : 2}
                // Thousands of samples: a dot per point is a smear, and the
                // hover layer already reports exact values. A points-only
                // series is the exception: its dots are the message.
                dot={s.points ? { r: 3, fill: `var(--series-${s.slot})`, strokeWidth: 0 } : false}
                activeDot={{ r: 4, strokeWidth: 0 }}
                isAnimationActive={false}
                connectNulls={false}
              />
            ))}
            {directLabels && <EndLabels rows={rows} series={series} xKey={xKey} />}
          </LineChart>
        </ResponsiveContainer>
      </div>

      {bands.length > 0 && (
        <ul className="mt-2 grid gap-1 text-xs" aria-label="Shaded stretches">
          {bands.map((b, i) => (
            <li key={i} className="flex items-baseline gap-2">
              <span aria-hidden="true" className="inline-block h-2.5 w-4 shrink-0 rounded-sm"
                    style={{ background: b.color, opacity: Math.max(b.depth, 0.3) }} />
              <span>{formatX(b.x1, xFormat)}–{formatX(b.x2, xFormat)}: {b.label}</span>
            </li>
          ))}
        </ul>
      )}

      <figcaption className="mt-2 flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-[var(--muted)]">
          {rows.length.toLocaleString()} points plotted
        </span>
        <button
          type="button"
          onClick={() => setShowTable((open) => !open)}
          aria-expanded={showTable}
          aria-controls={tableId}
          className="inline-flex min-h-11 items-center rounded-md border border-[var(--border)] px-3 text-xs"
        >
          {showTable ? "Hide table" : "Show as table"}
        </button>
      </figcaption>

      {/* The relief the palette's contrast WARN obliges. Also the accessible
          path to exact figures, which a chart cannot give. */}
      {showTable && (
        <div id={tableId} className="mt-3 max-h-80 overflow-auto rounded-lg border border-[var(--border)]">
          <table className="w-full border-collapse text-left text-xs">
            <thead className="sticky top-0 bg-[var(--surface-2)]">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">
                  {xLabel ?? xKey}
                </th>
                {series.map((s) => (
                  <th key={s.key} scope="col" className="px-3 py-2 font-medium">
                    {s.label}
                    {s.unit ? ` (${s.unit})` : ""}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i} className="border-t border-[var(--border)]">
                  <td className="tabular px-3 py-1.5">
                    {formatX(row[xKey], xFormat)}
                  </td>
                  {series.map((s) => (
                    <td key={s.key} className="tabular px-3 py-1.5">
                      {formatValue(row[s.key], digits)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </figure>
  );
}
