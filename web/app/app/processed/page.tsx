import Link from "next/link";
import {
  getPipelineResults,
  getProcessedOverview,
  getSessionResult,
  getStretchReadings,
} from "@/lib/queries";
import { FindingsList } from "@/components/processing/findings";
import { EmptyState, Stat, StatusBadge } from "@/components/ui/states";
import { PageHeader } from "@/components/ui/page-header";
import { LocalTime } from "@/components/ui/local-time";
import { Owner } from "@/components/ui/owner";
import { FLIGHTS, processedPath } from "@/lib/routes";
import { SEVERITY, excerptsFor, readTracks, severityOf } from "@/lib/pipeline";

export const metadata = { title: "Processed data" };

/**
 * Processed data — what the data pipeline made of every flight (the owner,
 * 2026-10-09: "a separate processed data page for all this"): every anomaly
 * it found, each with its readings and what it means; then every session
 * with processed flights,
 * each opening onto that session's processed data
 * (app/app/processed/[id]/page.tsx).
 *
 * A flight with nothing anomalous is still processed, and said to be — "no
 * anomalies" and "not processed" are different answers.
 */

/** How many anomalies the list shows in full; the page says if there are more. */
const SHOWN = 100;

function Note({ children }: { children: React.ReactNode }) {
  return (
    <p className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6 text-sm">
      {children}
    </p>
  );
}

/** What counts as an anomaly — the classifier's own thresholds
 *  (backend/agent/dronedeck/pipeline/stages/classify/blocks.py). */
function WhatCounts() {
  return (
    <details className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-4 text-sm">
      <summary className="min-h-11 cursor-pointer content-center font-semibold">
        What counts as an anomaly
      </summary>
      <div className="mt-2 space-y-2">
        <p>
          The pipeline cleans each flight&apos;s readings, then compares them with what was expected:
          the temperature against the sensor&apos;s own cooling curve for that flight, and the pressure
          against a straight line once height is allowed for. A stretch becomes an anomaly when it
          stays away from that expectation for at least 20 readings (two seconds), by at least four
          times the flight&apos;s own noise <em>and</em> at least 0.8 °C or 0.2 hPa. The first three
          seconds after take-off are left out while the sensor settles.
        </p>
        <p>
          A flight where nothing departed that far is processed and normal — on a calm flight that
          is the expected answer. Severity grows with the size of the departure: slight, then a
          warning at twice the minimum, critical at five times.
        </p>
      </div>
    </details>
  );
}

export default async function ProcessedPage() {
  const overview = await getProcessedOverview(SHOWN);

  if (!overview.migrated) {
    return (
      <div className="space-y-8">
        <PageHeader title="Processed data" />
        <Note>
          Pipeline results are not on the web yet: the database has no pipeline tables. Apply
          migration 20261009000016_pipeline_results.sql; the laptop keeps every result and uploads it
          once they exist.
        </Note>
      </div>
    );
  }

  const { flights, sessions, findings, findingsTotal } = overview;
  if (flights.length === 0) {
    return (
      <div className="space-y-8">
        <PageHeader title="Processed data" description="What the data pipeline made of each flight." />
        <EmptyState
          title="No flight has been processed yet"
          hint="Leave Process flights (DPP) on at the top of the desktop app's Control page: each flight is processed after it lands and the result uploads here."
        />
        <WhatCounts />
      </div>
    );
  }

  // The evidence for each anomaly shown: its readings and its flight's
  // tracks (what was expected).
  const flightIds = [...new Set(findings.map((f) => f.flight_id).filter((x): x is string => !!x))];
  const ownSessions = [...new Set(findings.filter((f) => f.scope === "session" && f.session_id)
    .map((f) => f.session_id as string))];
  const [readings, results, owns] = await Promise.all([
    getStretchReadings(findings),
    getPipelineResults(flightIds),
    Promise.all(ownSessions.map((sid) => getSessionResult(sid))),
  ]);
  // A flight's tracks by flight id; a session's own by session id.
  const tracks = new Map([
    ...results.rows.map((r) => [r.flight_id, readTracks(r.tracks)] as const),
    ...owns.flatMap((o) => (o.row ? [[o.row.session_id, readTracks(o.row.tracks)] as const] : [])),
  ]);
  const excerpts = excerptsFor(findings, (id) => tracks.get(id) ?? [], readings);

  const count = (severity: string) => findings.filter((f) => severityOf(f.severity) === severity).length;
  const sessionOf = new Map(sessions.map((s) => [s.id, s]));

  return (
    <div className="space-y-10">
      <PageHeader
        title="Processed data"
        description="What the data pipeline made of every flight: cleaned readings and each stretch that departed from what was expected — with what it means."
      />

      <section aria-label="Summary" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <Stat label="Flights processed" value={flights.length.toLocaleString()} />
        <Stat label="Sessions" value={sessions.length.toLocaleString()} />
        <Stat label="Anomalies" value={findingsTotal.toLocaleString()} />
        <Stat label="Critical" value={count("critical").toLocaleString()} />
        <Stat label="Warning · slight" value={`${count("warning")} · ${count("info")}`} />
      </section>

      <section aria-labelledby="anomalies-heading" className="space-y-3">
        <h2 id="anomalies-heading" className="text-lg font-semibold text-[var(--heading)]">
          All anomalies
        </h2>
        {findings.length === 0 ? (
          <Note>
            None of the {flights.length.toLocaleString()} processed flight{flights.length === 1 ? "" : "s"}{" "}
            departed from what was expected. Every one is processed — open a session below for its
            cleaned readings and the expected curves they were checked against.
          </Note>
        ) : (
          <>
            <p className="text-sm text-[var(--muted)]">
              Worst first. The readings are the evidence.
              {findingsTotal > findings.length && ` Showing ${findings.length} of ${findingsTotal}.`}
            </p>
            <FindingsList
              findings={findings}
              excerpts={excerpts}
              flightOf={(id) => ({ label: "Open the flight", href: `${FLIGHTS}/${id}` })}
              moreOf={(f) => f.session_id
                ? { label: "See it in the session's processed data →", href: `${processedPath(f.session_id)}#finding-${f.id}` }
                : undefined}
            />
          </>
        )}
        <WhatCounts />
      </section>

      <section aria-labelledby="sessions-heading" className="space-y-3">
        <h2 id="sessions-heading" className="text-lg font-semibold text-[var(--heading)]">
          Sessions with processed flights
        </h2>
        <div className="overflow-x-auto [contain:paint] rounded-lg border border-[var(--border)]">
          <table className="w-full border-collapse text-left text-sm">
            <caption className="sr-only">Sessions with processed flights, newest first</caption>
            <thead className="bg-[var(--surface-2)]">
              <tr>
                <th scope="col" className="px-4 py-3 font-medium">Started</th>
                <th scope="col" className="px-4 py-3 font-medium">Run by</th>
                <th scope="col" className="px-4 py-3 font-medium">Mode</th>
                <th scope="col" className="px-4 py-3 font-medium">Processed</th>
                <th scope="col" className="px-4 py-3 font-medium">Anomalies</th>
                <th scope="col" className="px-4 py-3 font-medium"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => {
                const sev = s.worst ? SEVERITY[severityOf(s.worst)] : null;
                return (
                  <tr key={s.id} className="border-t border-[var(--border)]">
                    <th scope="row" className="px-4 py-3 font-normal"><LocalTime iso={s.started_at} /></th>
                    <td className="px-4 py-3"><Owner who={s} /></td>
                    <td className="px-4 py-3">{s.mode_at_start ?? "—"}</td>
                    <td className="tabular px-4 py-3">
                      {s.processed} of {s.flights} flight{s.flights === 1 ? "" : "s"}
                      <span className="block text-xs text-[var(--muted)]">
                        {s.ownResult ? "and the session itself" : "the session itself not yet"}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {sev ? <StatusBadge status={sev.status} label={`${s.findings} · worst ${sev.label.toLowerCase()}`} />
                        : <StatusBadge status="good" label="None" />}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <Link href={processedPath(s.id)} className="inline-flex min-h-11 items-center underline underline-offset-4">
                        Open<span className="sr-only">{" "}processed data of the session from <LocalTime iso={s.started_at} /></span>
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {flights.some((f) => !f.session_id || !sessionOf.has(f.session_id)) && (
          <p className="text-xs text-[var(--muted)]">
            Some processed flights belong to no session on the web yet; they appear here once their
            session uploads.
          </p>
        )}
      </section>
    </div>
  );
}
