import Link from "next/link";
import { notFound } from "next/navigation";
import {
  getFindings,
  getFlightsTelemetry,
  getPipelineResults,
  getSession,
  getSessionFindings,
  getSessionFlights,
  getSessionResult,
  getSessionSamples,
} from "@/lib/queries";
import { FindingsList } from "@/components/processing/findings";
import { FlightProcessing } from "@/components/processing/flight-processing";
import { ErrorTable } from "@/components/processing/error-table";
import { RawReadings, SESSION_COLUMNS } from "@/components/ui/raw-readings";
import { Stat, StatusBadge } from "@/components/ui/states";
import { PageHeader } from "@/components/ui/page-header";
import { LocalTime } from "@/components/ui/local-time";
import { OwnerStat } from "@/components/ui/owner";
import { FLIGHTS, PROCESSED, SESSIONS } from "@/lib/routes";
import {
  SEVERITY,
  excerptsFor,
  marksOf,
  readFlags,
  readTracks,
  severityOf,
} from "@/lib/pipeline";

export const metadata = { title: "Processed data · session" };

/**
 * One session's processed data — everything the pipeline made of it, in Auto
 * and Manual alike (the owner, 2026-10-09: "as long as a session is started we
 * process whatever data comes"):
 *
 *   the anomalies     every finding of the session — its flights' and its own
 *                     on the ground — with its readings and meaning
 *   each flight       what ran, a verdict per point, raw vs clean against what
 *                     was expected, what went wrong (the error table), and
 *                     every reading with the marks tinted
 *   the session       its own result: the one-a-second samples around the
 *                     flights, judged on the ground — the same four parts
 *
 * The session page (app/app/sessions/[id]) still holds everything in the
 * session; this is the pipeline's view of it.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6 text-sm">{children}</p>
  );
}

export default async function ProcessedSessionPage(props: PageProps<"/app/processed/[id]">) {
  const { id } = await props.params;
  if (!UUID.test(id)) notFound();

  const [session, flights, own, ownFindings, samples] = await Promise.all([
    getSession(id),
    getSessionFlights(id),
    getSessionResult(id),
    getSessionFindings(id),
    getSessionSamples(id),
  ]);
  if (!session) notFound();
  const flightIds = flights.map((f) => f.id);
  const [results, findings, telemetry] = await Promise.all([
    getPipelineResults(flightIds),
    getFindings(flightIds),
    getFlightsTelemetry(flightIds),
  ]);
  const resultOf = new Map(results.rows.map((r) => [r.flight_id, r]));
  const processed = flights.filter((f) => resultOf.has(f.id));
  const notProcessed = flights.filter((f) => !resultOf.has(f.id));
  const flightNumber = new Map(flights.map((f, i) => [f.id, i + 1]));
  const sampleRows = samples.rows.map((r) => ({ ...r, index: r.seq }));
  const all = [...findings.rows, ...ownFindings.rows];

  // Each anomaly's readings: its flight's telemetry, or the session's samples.
  const readings = Object.fromEntries([
    ...findings.rows.map((f) => [f.id, (telemetry[f.flight_id ?? ""] ?? [])
      .filter((r) => r.index >= f.start_index && r.index <= f.end_index)]),
    ...ownFindings.rows.map((f) => [f.id, sampleRows
      .filter((r) => r.seq >= f.start_index && r.seq <= f.end_index)]),
  ]);
  const excerpts = excerptsFor(all, (key) => key === id
    ? readTracks(own.row?.tracks ?? []) : readTracks(resultOf.get(key)?.tracks ?? []), readings);
  const sessionMarks = marksOf(ownFindings.rows, own.row ? readFlags(own.row.flags) : [],
                               samples.rows, "seq");
  const bySeverity = (s: string) => all.filter((f) => severityOf(f.severity) === s).length;
  const nothingProcessed = processed.length === 0 && !own.row;

  return (
    <div className="space-y-10">
      <div className="space-y-4">
        <p className="flex flex-wrap gap-x-6 text-sm">
          <Link href={PROCESSED} className="inline-flex min-h-11 items-center underline underline-offset-4">
            ← Processed data
          </Link>
          <Link href={`${SESSIONS}/${id}`} className="inline-flex min-h-11 items-center underline underline-offset-4">
            The whole session →
          </Link>
        </p>
        <PageHeader
          title={<>Processed data · <LocalTime iso={session.started_at} /></>}
          description="What the data pipeline made of this session — its flights, and the readings around them."
        />
      </div>

      <section aria-label="Summary" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Stat label="Flights processed" value={`${processed.length} of ${flights.length}`}
              hint={own.row ? "and the session itself" : "the session itself not yet"} />
        <Stat label="Anomalies" value={all.length
          ? `${all.length} · ${bySeverity("critical")} critical, ${bySeverity("warning")} warning`
          : "None"} />
        <OwnerStat label="Run by" who={session} />
      </section>

      {!results.migrated || !findings.migrated ? (
        <Note>
          Pipeline results are not on the web yet: the database has no pipeline tables. Apply
          migration 20261009000016_pipeline_results.sql.
        </Note>
      ) : nothingProcessed ? (
        <Note>
          Nothing in this session has been processed yet. Leave Process flights (DPP) on at the top of
          the desktop app&apos;s Control page: each flight is processed when it lands and the session when
          it ends, and the results upload here.
        </Note>
      ) : (
        <>
          <section aria-labelledby="anomalies-heading" className="space-y-3">
            <h2 id="anomalies-heading" className="text-lg font-semibold text-[var(--heading)]">
              Anomalies in this session
            </h2>
            {all.length === 0 ? (
              <Note>
                Nothing departed from what was expected — in the {processed.length} processed flight
                {processed.length === 1 ? "" : "s"}{own.row ? " or on the ground around them" : ""}. The
                readings below are cleaned and checked; there is simply nothing to flag.
              </Note>
            ) : (
              <FindingsList
                findings={all}
                excerpts={excerpts}
                flightOf={(flightId) => flightNumber.has(flightId)
                  ? { label: `Flight ${flightNumber.get(flightId)}`, href: `#flight-${flightId}` }
                  : undefined}
              />
            )}
          </section>

          {processed.map((f) => {
            const mine = findings.rows.filter((x) => x.flight_id === f.id);
            const r = resultOf.get(f.id)!;
            const rows = telemetry[f.id] ?? [];
            const flightMarks = marksOf(mine, readFlags(r.flags), rows);
            const sev = r.worst_severity ? SEVERITY[severityOf(r.worst_severity)] : null;
            return (
              <section key={f.id} id={`flight-${f.id}`} aria-labelledby={`flight-${f.id}-heading`}
                       className="scroll-mt-24 space-y-6 border-t border-[var(--border)] pt-6">
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                  <h2 id={`flight-${f.id}-heading`} className="text-lg font-semibold text-[var(--heading)]">
                    Flight {flightNumber.get(f.id)}{" "}
                    <span className="text-base font-normal text-[var(--muted)]">
                      · <LocalTime iso={f.started_at} /> · {f.program ?? f.mode ?? "flight"}
                    </span>
                  </h2>
                  <span className="flex flex-wrap items-center gap-4">
                    {sev ? <StatusBadge status={sev.status} label={`${mine.length} anomal${mine.length === 1 ? "y" : "ies"} · worst ${sev.label.toLowerCase()}`} />
                      : <StatusBadge status="good" label="No anomalies" />}
                    <Link href={`${FLIGHTS}/${f.id}`} className="inline-flex min-h-11 items-center text-sm underline underline-offset-4">
                      Flight page
                    </Link>
                  </span>
                </div>
                <FlightProcessing result={r} telemetry={rows} findings={mine} />
                <div className="space-y-3">
                  <h3 className="text-base font-semibold">What went wrong</h3>
                  <ErrorTable marks={flightMarks} rows={rows} table={`flight-${f.id}-readings`}
                              unit={f.temp_unit} />
                </div>
                <div className="space-y-3">
                  <h3 className="text-base font-semibold">Every reading</h3>
                  <RawReadings rows={rows} unit={f.temp_unit} marks={flightMarks}
                               id={`flight-${f.id}-readings`} />
                </div>
              </section>
            );
          })}

          {notProcessed.length > 0 && (
            <Note>
              Not processed: {notProcessed.map((f) => `Flight ${flightNumber.get(f.id)}`).join(", ")} — flown
              with Process flights (DPP) off, or still waiting. Press Process this flight in the desktop
              app to add {notProcessed.length === 1 ? "it" : "them"}.
            </Note>
          )}

          <section id="session-own" aria-labelledby="own-heading"
                   className="scroll-mt-24 space-y-6 border-t border-[var(--border)] pt-6">
            <h2 id="own-heading" className="text-lg font-semibold text-[var(--heading)]">
              Around the flights — the session, one reading a second
            </h2>
            {!own.migrated ? (
              <Note>
                The session&apos;s own results are not on the web yet: apply migration
                20261009000018_session_results.sql; the laptop keeps them until it can upload.
              </Note>
            ) : !own.row ? (
              <Note>
                The session itself has not been processed yet: it is processed when it ends, with
                Process flights (DPP) on, and uploads here.
              </Note>
            ) : (
              <>
                <FlightProcessing result={own.row} telemetry={sampleRows} findings={ownFindings.rows}
                                  over="session" />
                <div className="space-y-3">
                  <h3 className="text-base font-semibold">What went wrong</h3>
                  <ErrorTable marks={sessionMarks} rows={samples.rows} indexKey="seq"
                              table="session-readings" unit="C" what="session" />
                </div>
                <div className="space-y-3">
                  <h3 className="text-base font-semibold">Every reading</h3>
                  <RawReadings rows={samples.rows} unit="C" columns={SESSION_COLUMNS} indexKey="seq"
                               what="session" marks={sessionMarks} id="session-readings" />
                </div>
              </>
            )}
          </section>
        </>
      )}

    </div>
  );
}
