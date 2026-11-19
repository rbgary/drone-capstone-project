import Link from "next/link";
import type { FindingRow } from "@/lib/queries";
import { GROUPS, SEVERITY, clock, groupColor, groupTint, severityOf, unitSymbol, type ExcerptRow } from "@/lib/pipeline";
import { MarkShape } from "@/components/ui/mark-legend";

/**
 * What the data pipeline found: each stretch of a flight whose temperature or
 * pressure departed from what was expected, judged and put into words by the
 * agent (stages/interpret/findings.py) — worst first.
 *
 * THE READINGS ARE THE EVIDENCE. There is no camera (2026-11-15): the frames
 * once shown beside a finding, and its camera note, are gone; photos taken by
 * hand will have a section of their own on the session page.
 *
 * WITH AN EXCERPT (the Processed data pages), the card also carries the
 * stretch's own readings — the value stored, what the pipeline measured after
 * any correction, what it expected, and the difference — so the evidence and
 * its meaning sit in one place.
 */

/** A stretch's readings, for the card (lib/pipeline excerptOf). */
export type Excerpt = { rows: ExcerptRow[]; total: number; rawLabel: string };

function Readings({ f, excerpt }: { f: FindingRow; excerpt: Excerpt }) {
  const unit = unitSymbol(f.unit);
  const digits = f.signal === "pressure" ? 2 : 1;
  const n = (v: number | null) => (v === null ? "—" : v.toFixed(digits));
  if (excerpt.rows.length === 0) {
    return <p className="text-xs text-[var(--muted)]">The readings of this stretch are not on the web yet.</p>;
  }
  // The stored value is the measured one for temperature; said once, not twice.
  const stored = excerpt.rows.some((r) => n(r.raw) !== n(r.measured));
  return (
    <div className="space-y-1">
      <div className="overflow-x-auto [contain:paint] rounded-md border border-[var(--border)]">
        <table className="w-full border-collapse text-left text-xs">
          <caption className="sr-only">
            Readings in this stretch: {excerpt.rows.length} of {excerpt.total}, in {unit}
          </caption>
          <thead className="bg-[var(--surface-2)]">
            <tr>
              <th scope="col" className="px-2 py-1.5 font-medium">Reading</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Time</th>
              {stored && <th scope="col" className="px-2 py-1.5 font-medium">{excerpt.rawLabel} ({unit})</th>}
              <th scope="col" className="px-2 py-1.5 font-medium">Measured ({unit})</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Expected ({unit})</th>
              <th scope="col" className="px-2 py-1.5 font-medium">Difference</th>
            </tr>
          </thead>
          <tbody>
            {excerpt.rows.map((r) => {
              const d = r.measured !== null && r.expected !== null ? r.measured - r.expected : null;
              return (
                <tr key={r.index} className="border-t border-[var(--border)]">
                  <th scope="row" className="tabular px-2 py-1 font-normal">{r.index}</th>
                  <td className="tabular px-2 py-1">{clock(r.t)}</td>
                  {stored && <td className="tabular px-2 py-1">{n(r.raw)}</td>}
                  <td className="tabular px-2 py-1">{n(r.measured)}</td>
                  <td className="tabular px-2 py-1">{n(r.expected)}</td>
                  <td className="tabular px-2 py-1 font-semibold">{d === null ? "—" : `${d > 0 ? "+" : ""}${d.toFixed(digits)}`}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {excerpt.total > excerpt.rows.length && (
        <p className="text-xs text-[var(--muted)]">
          {excerpt.rows.length} of the stretch&apos;s {excerpt.total} readings: the first, the last, the one
          furthest from what was expected, and evenly between.
        </p>
      )}
    </div>
  );
}

function Change({ f }: { f: FindingRow }) {
  const unit = unitSymbol(f.unit);
  const digits = f.signal === "pressure" ? 2 : 1;
  if (f.observed === null || f.expected === null) return null;
  return (
    <>
      {f.observed.toFixed(digits)} {unit} measured against {f.expected.toFixed(digits)} {unit}{" "}
      expected — {f.delta !== null && f.delta > 0 ? "+" : ""}{f.delta?.toFixed(digits)} {unit}
      {f.z !== null ? (f.z >= 100 ? ", far beyond the flight's noise"
        : `, ${Math.round(f.z)}× the flight's noise`) : ""}
    </>
  );
}

function Where({ f }: { f: FindingRow }) {
  const points = f.point_ids.length ? f.point_ids.join(", ") : "Between inspection points";
  const place = f.x_m !== null && f.y_m !== null && f.z_m !== null
    ? ` · (${f.x_m.toFixed(2)}, ${f.y_m.toFixed(2)}) m, ${f.z_m.toFixed(2)} m up`
    : " · position not measured";
  return <>{points}{place}</>;
}

export function FindingCard({ f, flight, excerpt, more }: {
  f: FindingRow;
  /** On a session page: which flight it is from, and its page. */
  flight?: { label: string; href: string };
  /** The stretch's readings, shown under the card's facts. */
  excerpt?: Excerpt;
  /** Where else to look — the session's processed data, say. */
  more?: { label: string; href: string };
}) {
  const sev = SEVERITY[severityOf(f.severity)];
  const group = f.signal === "pressure" ? "pressure" as const : "temperature" as const;
  return (
    <article id={`finding-${f.id}`}
             style={{ borderLeftColor: groupColor(f.signal === "pressure" ? "pressure" : "temperature") }}
             className="scroll-mt-24 space-y-3 rounded-lg border border-l-4 border-[var(--border)] bg-[var(--surface)] p-4">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-base font-semibold">{f.title}</h3>
        <span className="inline-flex items-center gap-1.5 rounded border-l-4 px-2 py-1 text-xs font-medium"
              style={{ borderLeftColor: groupColor(group), background: groupTint(group, severityOf(f.severity)) }}>
          <MarkShape mark={{ group, severity: severityOf(f.severity) }} />
          {GROUPS[group].label} · {sev.label}
        </span>
      </header>
      <p className="text-sm">{f.sentence}</p>
      <dl className="grid gap-x-6 gap-y-1 text-sm sm:grid-cols-[auto_1fr]">
        <dt className="text-[var(--muted)]">When</dt>
        <dd>
          {clock(f.t_start_s)}–{clock(f.t_end_s)} into the flight
          {flight && (
            <>
              {" · "}
              <Link href={flight.href.startsWith("#") ? flight.href : `${flight.href}#finding-${f.id}`}
                    className="underline underline-offset-4">
                {flight.label}
              </Link>
            </>
          )}
        </dd>
        <dt className="text-[var(--muted)]">Where</dt>
        <dd><Where f={f} /></dd>
        <dt className="text-[var(--muted)]">Change</dt>
        <dd><Change f={f} /></dd>
      </dl>
      {excerpt && <Readings f={f} excerpt={excerpt} />}
      {more && (
        <p className="text-sm">
          <Link href={more.href} className="inline-flex min-h-11 items-center underline underline-offset-4">
            {more.label}
          </Link>
        </p>
      )}
    </article>
  );
}

export function FindingsList({ findings, flightOf, excerpts, moreOf }: {
  findings: FindingRow[];
  flightOf?: (flightId: string) => { label: string; href: string } | undefined;
  excerpts?: Record<string, Excerpt>;
  moreOf?: (f: FindingRow) => { label: string; href: string } | undefined;
}) {
  return (
    <div className="space-y-3">
      {findings.map((f) => (
        <FindingCard key={f.id} f={f}
                     flight={f.flight_id ? flightOf?.(f.flight_id) : undefined}
                     excerpt={excerpts?.[f.id]} more={moreOf?.(f)} />
      ))}
    </div>
  );
}
