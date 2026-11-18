import Link from "next/link";
import { notFound } from "next/navigation";
import {
  getFindings,
  getFlightsTelemetry,
  getPipelineResults,
  getSession,
  getSessionEvents,
  getSessionFlights,
  getSessionFindings,
  getSessionResult,
  getSessionSamples,
  getZones,
} from "@/lib/queries";
import { ErrorTable } from "@/components/processing/error-table";
import { FindingsList } from "@/components/processing/findings";
import { FlightProcessing } from "@/components/processing/flight-processing";
import { SEVERITY, excerptsFor, marksOf, readFlags, readTracks, severityOf } from "@/lib/pipeline";
import { Stat, StatusBadge } from "@/components/ui/states";
import { PageHeader } from "@/components/ui/page-header";
import { TimeSeries } from "@/components/ui/time-series";
import { FlightPath } from "@/components/ui/flight-path";
import { RawReadings, SESSION_COLUMNS } from "@/components/ui/raw-readings";
import { FLIGHTS, SESSIONS, processedPath } from "@/lib/routes";
import {
  elapsedSeconds,
  flightDuration,
  flightStatusLabel,
  flightTone,
} from "@/lib/flight-format";
import { LocalTime } from "@/components/ui/local-time";
import { Owner, OwnerStat } from "@/components/ui/owner";

export const metadata = { title: "Session" };

/**
 * One session — EVERYTHING in it (the owner, 2026-10-09: "the session is a
 * superset of the flight page"): what the data pipeline found in any of its
 * flights, each flight's processing (raw vs clean, expected, the stretches
 * that departed), its flights, the session's own vitals (session_samples: one a second, Start to End session,
 * flying or not), its events, and the raw rows of the session AND of every
 * flight in it.
 *
 * PARITY: web/app/app/flights/[id]/page.tsx
 */

/** Matches `getSessionSamples`' default cap: 10 000 s, about 2¾ hours. */
const SAMPLE_CAP = 10000;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;


function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6 text-sm text-[var(--muted)]">
      {children}
    </p>
  );
}

export default async function SessionPage(props: PageProps<"/app/sessions/[id]">) {
  const { id } = await props.params;
  // A malformed id is a missing page, not a database error.
  if (!UUID.test(id)) notFound();

  const [session, samples, flights, events, zones] = await Promise.all([
    getSession(id),
    getSessionSamples(id, SAMPLE_CAP),
    getSessionFlights(id),
    getSessionEvents(id),
    getZones(),
  ]);
  if (!session) notFound();
  const flightIds = flights.map((f) => f.id);
  const [results, findings, telemetry, own, ownFindings] = await Promise.all([
    getPipelineResults(flightIds),
    getFindings(flightIds),
    getFlightsTelemetry(flightIds),
    getSessionResult(id),
    getSessionFindings(id),
  ]);
  const resultOf = new Map(results.rows.map((r) => [r.flight_id, r]));
  const flightNumber = new Map(flights.map((f, i) => [f.id, i + 1]));
  const flightOf = (flightId: string) => flightNumber.has(flightId)
    ? { label: `Flight ${flightNumber.get(flightId)} of the session`, href: `${FLIGHTS}/${flightId}` }
    : undefined;
  const processed = flights.filter((f) => resultOf.has(f.id)).length;
  const rows = samples.rows;
  // The session's own result: its samples around the flights (scope "session").
  const ownRows = ownFindings.rows;
  const sampleRows = rows.map((r) => ({ ...r, index: r.seq }));
  const excerpts = excerptsFor([...findings.rows, ...ownRows],
    (key) => key === id ? readTracks(own.row?.tracks ?? []) : readTracks(resultOf.get(key)?.tracks ?? []),
    Object.fromEntries([
      ...findings.rows.map((f) => [f.id, (telemetry[f.flight_id ?? ""] ?? [])
        .filter((r) => r.index >= f.start_index && r.index <= f.end_index)]),
      ...ownRows.map((f) => [f.id, sampleRows
        .filter((r) => r.seq >= f.start_index && r.seq <= f.end_index)]),
    ]));
  const sessionMarks = marksOf(ownRows, own.row ? readFlags(own.row.flags) : [], rows, "seq");
  const flightMarks = (flightId: string) => marksOf(
    findings.rows.filter((x) => x.flight_id === flightId),
    readFlags(resultOf.get(flightId)?.flags ?? []), telemetry[flightId] ?? []);
  const first = rows[0]?.recorded_at;
  // Only the columns the charts use cross into the client bundle.
  const series = rows.map((r) => ({
    t: first ? Math.round(elapsedSeconds(r.recorded_at, first)) : 0,
    raw_temp: r.raw_temp,
    height_m: r.height_m,
    battery_v: r.battery_v,
    station_pressure_hpa: r.station_pressure_hpa,
  }));
  // A path only from samples the drone trusted: an unmeasured base station
  // gives a drifting estimate, which is not a place (agent position_trusted).
  const path = rows.filter((r) => r.positioned).map((r) => ({ x_m: r.x_m, y_m: r.y_m }));

  const noVitals = !samples.migrated ? (
    <Note>
      Session vitals are not on the web yet: the database has no session_samples table. Apply
      migration 20261006000013_session_samples.sql; the laptop keeps every reading and uploads them
      once it exists.
    </Note>
  ) : rows.length === 0 ? (
    <Note>
      No vitals uploaded for this session yet. The laptop records one sample a second from Start
      session to End session and uploads them when it is online.
    </Note>
  ) : null;

  return (
    <div className="space-y-10">
      <div className="space-y-4">
        <p className="text-sm">
          <Link href={SESSIONS} className="inline-flex min-h-11 items-center underline underline-offset-4">
            ← All sessions
          </Link>
        </p>
        <PageHeader
          title={<>Session · <LocalTime iso={session.started_at} /></>}
          description={
            <StatusBadge status={session.ended_at ? "good" : "warning"}
                         label={session.ended_at ? `Ended${session.end_reason ? ` — ${session.end_reason}` : ""}` : "Open"} />
          }
        />
      </div>

      {rows.length >= SAMPLE_CAP && (
        <p role="status" className="rounded-lg border border-[var(--status-warning)] bg-[var(--surface)] p-3 text-sm">
          <span aria-hidden="true">▲ </span>
          Showing the first {SAMPLE_CAP.toLocaleString()} samples. This session recorded more; the
          full record is samples.csv in the session&apos;s folder on the laptop.
        </p>
      )}

      <section aria-label="Summary" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Duration" value={flightDuration(session.started_at, session.ended_at)} />
        <Stat label="Flights" value={String(session.flight_count)} />
        <Stat label="Vitals" value={rows.length.toLocaleString()} hint="one sample a second" />
        <OwnerStat label="Run by" who={session} />
        <Stat label="Drone" value={session.drone} />
        <Stat label="Mode at start" value={session.mode_at_start} />
        <Stat label="Positioned" value={rows.length ? `${Math.round((path.length / rows.length) * 100)}%` : null}
              hint="of samples with a trusted position" />
      </section>

      <section aria-labelledby="findings-heading" className="space-y-3">
        <h2 id="findings-heading" className="text-lg font-semibold text-[var(--heading)]">
          What the pipeline found
        </h2>
        {!results.migrated || !findings.migrated ? (
          <Note>
            Pipeline results are not on the web yet: the database has no pipeline tables. Apply
            migration 20261009000016_pipeline_results.sql; the laptop keeps every result and uploads
            it once they exist.
          </Note>
        ) : flights.length === 0 ? (
          <Note>No flights in this session, so nothing was processed.</Note>
        ) : processed === 0 ? (
          <Note>
            None of this session&apos;s {flights.length} flight{flights.length === 1 ? " has" : "s have"} been
            processed. Leave Process flights (DPP) on at the top of the desktop app&apos;s Control page, or
            press Process this flight there; the result uploads here.
          </Note>
        ) : findings.rows.length === 0 ? (
          <Note>
            Nothing departed from what was expected in the {processed} processed flight
            {processed === 1 ? "" : "s"}
            {processed < flights.length ? ` (${flights.length - processed} not processed yet)` : ""}.
          </Note>
        ) : (
          <>
            <p className="text-sm text-[var(--muted)]">
              {findings.rows.length} finding{findings.rows.length === 1 ? "" : "s"} in {processed} processed
              flight{processed === 1 ? "" : "s"}{processed < flights.length
                ? ` — ${flights.length - processed} not processed yet` : ""}. The readings are the
              evidence.
            </p>
            <FindingsList findings={findings.rows} flightOf={flightOf} excerpts={excerpts} />
          </>
        )}
        {ownRows.length > 0 && (
          <>
            <h3 className="text-base font-semibold">On the ground, around the flights</h3>
            <FindingsList findings={ownRows} excerpts={excerpts} />
          </>
        )}
        {(processed > 0 || own.row) && (
          <p className="text-sm">
            <Link href={processedPath(id)} className="inline-flex min-h-11 items-center underline underline-offset-4">
              This session&apos;s processed data, on its own page →
            </Link>
          </p>
        )}
      </section>

      {results.rows.length > 0 && (
        <section aria-labelledby="processing-heading" className="space-y-6">
          <h2 id="processing-heading" className="text-lg font-semibold text-[var(--heading)]">
            Data processing, flight by flight
          </h2>
          {flights.filter((f) => resultOf.has(f.id)).map((f) => (
            <section key={f.id} aria-label={`Flight ${flightNumber.get(f.id)}`}
                     className="space-y-3 border-t border-[var(--border)] pt-4">
              <h3 className="text-base font-semibold">
                <Link href={`${FLIGHTS}/${f.id}`} className="underline underline-offset-4">
                  Flight {flightNumber.get(f.id)}
                </Link>{" "}
                <span className="font-normal text-[var(--muted)]">
                  · <LocalTime iso={f.started_at} /> · {f.program ?? f.mode ?? "flight"}
                </span>
              </h3>
              <FlightProcessing result={resultOf.get(f.id)!} telemetry={telemetry[f.id] ?? []}
                                findings={findings.rows.filter((x) => x.flight_id === f.id)} />
            </section>
          ))}
        </section>
      )}

      <section aria-labelledby="temp-heading" className="space-y-3">
        <h2 id="temp-heading" className="text-lg font-semibold text-[var(--heading)]">
          Session vitals: temperature (°C)
        </h2>
        <p className="text-sm text-[var(--muted)]">
          The barometer&apos;s own sensor, which the drone warms. Corrected room temperatures are
          worked out per flight — open a flight below for them.
        </p>
        {noVitals ?? (
          <TimeSeries rows={series} xKey="t" xLabel="Time (s)" xFormat="seconds" yLabel="°C"
                      series={[{ key: "raw_temp", label: "Sensor", slot: 1, unit: "°C" }]} />
        )}
      </section>

      {/* Different units, so different charts — never a second y-axis. */}
      {!noVitals && (
        <div className="grid gap-10 lg:grid-cols-2">
          <section aria-labelledby="alt-heading" className="space-y-3">
            <h2 id="alt-heading" className="text-lg font-semibold text-[var(--heading)]">
              Height above ground (m)
            </h2>
            <TimeSeries rows={series} xKey="t" xLabel="Time (s)" xFormat="seconds" yLabel="m" digits={3}
                        series={[{ key: "height_m", label: "Height", slot: 1, unit: "m" }]} />
          </section>

          <section aria-labelledby="battery-heading" className="space-y-3">
            <h2 id="battery-heading" className="text-lg font-semibold text-[var(--heading)]">
              Battery (V)
            </h2>
            <TimeSeries rows={series} xKey="t" xLabel="Time (s)" xFormat="seconds" yLabel="V"
                        series={[{ key: "battery_v", label: "Battery", slot: 1, unit: "V" }]} />
          </section>

          <section aria-labelledby="pressure-heading" className="space-y-3">
            <h2 id="pressure-heading" className="text-lg font-semibold text-[var(--heading)]">
              Station pressure (hPa)
            </h2>
            <TimeSeries rows={series} xKey="t" xLabel="Time (s)" xFormat="seconds" yLabel="hPa"
                        series={[{ key: "station_pressure_hpa", label: "Pressure", slot: 1, unit: "hPa" }]} />
          </section>

          <section aria-labelledby="path-heading" className="space-y-3">
            <h2 id="path-heading" className="text-lg font-semibold text-[var(--heading)]">
              Where the drone was
            </h2>
            <FlightPath rows={path} zones={zones} what="session" />
          </section>
        </div>
      )}

      <section aria-labelledby="flights-heading" className="space-y-3">
        <h2 id="flights-heading" className="text-lg font-semibold text-[var(--heading)]">
          Flights in this session
        </h2>
        {flights.length === 0 ? (
          <Note>No flights in this session — the drone stayed on the ground. Its vitals are above.</Note>
        ) : (
          <div className="overflow-x-auto [contain:paint] rounded-lg border border-[var(--border)]">
            <table className="w-full border-collapse text-left text-sm">
              <caption className="sr-only">Flights in this session, in order</caption>
              <thead className="bg-[var(--surface-2)]">
                <tr>
                  <th scope="col" className="px-4 py-3 font-medium">Started</th>
                  <th scope="col" className="px-4 py-3 font-medium">Status</th>
                  <th scope="col" className="px-4 py-3 font-medium">Duration</th>
                  <th scope="col" className="px-4 py-3 font-medium">What flew</th>
                  <th scope="col" className="px-4 py-3 font-medium">Flown by</th>
                  <th scope="col" className="px-4 py-3 font-medium">Processed</th>
                  <th scope="col" className="px-4 py-3 font-medium"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {flights.map((f) => (
                  <tr key={f.id} className="border-t border-[var(--border)]">
                    <th scope="row" className="px-4 py-3 font-normal"><LocalTime iso={f.started_at} /></th>
                    <td className="px-4 py-3">
                      <StatusBadge status={flightTone(f.status)} label={flightStatusLabel(f.status)} />
                    </td>
                    <td className="tabular px-4 py-3">{flightDuration(f.started_at, f.ended_at)}</td>
                    <td className="px-4 py-3">{f.program ?? f.mode ?? "—"}{f.abort_reason ? ` · ${f.abort_reason}` : ""}</td>
                    <td className="px-4 py-3"><Owner who={f} /></td>
                    <td className="px-4 py-3">
                      {(() => {
                        const r = resultOf.get(f.id);
                        if (!r) return <span className="text-[var(--muted)]">Not yet</span>;
                        if (!r.worst_severity) return <StatusBadge status="good" label="Nothing found" />;
                        const sev = SEVERITY[severityOf(r.worst_severity)];
                        return <StatusBadge status={sev.status}
                                            label={`${r.findings_count} found · worst ${sev.label.toLowerCase()}`} />;
                      })()}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <Link href={`${FLIGHTS}/${f.id}`} className="inline-flex min-h-11 items-center underline underline-offset-4">
                        Open<span className="sr-only">{" "}flight from <LocalTime iso={f.started_at} /></span>
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section aria-labelledby="events-heading" className="space-y-3">
        <h2 id="events-heading" className="text-lg font-semibold text-[var(--heading)]">What happened</h2>
        {events.length === 0 ? (
          <Note>No events recorded for this session (operators see the audit trail).</Note>
        ) : (
          <div tabIndex={0} role="region" aria-label="Session events"
               className="max-h-[28rem] overflow-auto rounded-lg border border-[var(--border)]">
            <table className="w-full border-collapse text-left text-sm">
              <caption className="sr-only">Session events, in order</caption>
              <thead className="sticky top-0 bg-[var(--surface-2)]">
                <tr>
                  <th scope="col" className="px-4 py-2 font-medium">When</th>
                  <th scope="col" className="px-4 py-2 font-medium">What</th>
                  <th scope="col" className="px-4 py-2 font-medium">Result</th>
                  <th scope="col" className="px-4 py-2 font-medium">Who</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.id} className="border-t border-[var(--border)]">
                    <th scope="row" className="whitespace-nowrap px-4 py-2 font-normal"><LocalTime iso={e.occurred_at} /></th>
                    <td className="px-4 py-2">{e.action.replaceAll("_", " ")}</td>
                    <td className="px-4 py-2">
                      <StatusBadge status={e.result === "ok" ? "good" : e.result === "refused" ? "warning" : "critical"}
                                   label={e.result} />
                    </td>
                    <td className="px-4 py-2">{e.actor_email ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section aria-labelledby="raw-heading" className="space-y-3">
        <h2 id="raw-heading" className="text-lg font-semibold text-[var(--heading)]">Raw readings</h2>
        <h3 className="text-base font-semibold">The session, one a second</h3>
        {noVitals ?? (
          <>
            {own.row && (
              <ErrorTable marks={sessionMarks} rows={rows} indexKey="seq" table="session-readings"
                          unit="C" what="session" />
            )}
            <RawReadings rows={rows} unit="C" columns={SESSION_COLUMNS} what="session" indexKey="seq"
                         marks={sessionMarks} id="session-readings" />
          </>
        )}
        {flights.map((f) => (
          <details key={f.id} className="rounded-lg border border-[var(--border)] p-3"
                   open={findings.rows.some((x) => x.flight_id === f.id)}>
            <summary className="min-h-11 cursor-pointer content-center text-sm font-semibold">
              Flight {flightNumber.get(f.id)} — {(telemetry[f.id] ?? []).length.toLocaleString()} readings, ten a
              second
            </summary>
            <div className="mt-3 space-y-4">
              {resultOf.has(f.id) && (
                <ErrorTable marks={flightMarks(f.id)} rows={telemetry[f.id] ?? []}
                            table={`flight-${f.id}-readings`} unit={f.temp_unit} />
              )}
              <RawReadings rows={telemetry[f.id] ?? []} unit={f.temp_unit} marks={flightMarks(f.id)}
                           id={`flight-${f.id}-readings`} />
            </div>
          </details>
        ))}
      </section>
    </div>
  );
}
