import Link from "next/link";
import { notFound } from "next/navigation";
import {
  getFlight,
  getFlightTelemetry,
  getFindings,
  getPipelineResults,
  getZones,
} from "@/lib/queries";
import { ErrorState, Stat, StatusBadge } from "@/components/ui/states";
import { PageHeader } from "@/components/ui/page-header";
import { TimeSeries } from "@/components/ui/time-series";
import { FlightPath } from "@/components/ui/flight-path";
import { RawReadings } from "@/components/ui/raw-readings";
import { FLIGHTS, processedPath } from "@/lib/routes";
import { excerptsFor, marksOf, readFlags, readTracks } from "@/lib/pipeline";
import { ErrorTable } from "@/components/processing/error-table";
import {
  elapsedSeconds,
  flightDuration,
  flightStatusLabel,
  flightTone,
} from "@/lib/flight-format";
import { LocalTime } from "@/components/ui/local-time";
import { FindingsList } from "@/components/processing/findings";
import { FlightProcessing } from "@/components/processing/flight-processing";
import { OwnerStat } from "@/components/ui/owner";

export const metadata = { title: "Flight" };

/** Matches `getFlightTelemetry`'s default cap. */
const TELEMETRY_CAP = 5000;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function FlightPage(props: PageProps<"/app/flights/[id]">) {
  const { id } = await props.params;

  // A malformed id is a missing page, not a database error: Postgres would
  // reject it as invalid uuid syntax and the operator would see "could not load".
  if (!UUID.test(id)) notFound();

  const [flight, telemetry, zones, results, findings] = await Promise.all([
    getFlight(id),
    getFlightTelemetry(id, TELEMETRY_CAP),
    getZones(),
    getPipelineResults([id]),
    getFindings([id]),
  ]);
  if (!flight) notFound();
  const result = results.rows[0] ?? null;
  // The anomalies, marked where they show: their readings in the table and
  // each card's own excerpt.
  const readingMarks = marksOf(findings.rows, result ? readFlags(result.flags) : [], telemetry);
  const excerpts = excerptsFor(findings.rows, () => (result ? readTracks(result.tracks) : []),
    Object.fromEntries(findings.rows.map((f) => [
      f.id, telemetry.filter((r) => r.index >= f.start_index && r.index <= f.end_index)])));

  const unit = flight.temp_unit;
  const first = telemetry[0]?.recorded_at;
  // Only the columns the charts use cross into the client bundle.
  const series = telemetry.map((r) => ({
    t: first ? Math.round(elapsedSeconds(r.recorded_at, first) * 10) / 10 : 0,
    raw_temp: r.raw_temp,
    corrected_temp: r.corrected_temp,
    ambient_est: r.ambient_est,
    // `z_m` is already metres above the ground reference captured at takeoff —
    // the agent subtracts it when it writes the row (telemetry/reader.py).
    // Subtracting it again here made every height wrong by the floor's offset.
    height_m: r.z_m,
    battery_v: r.battery_v,
    station_pressure_hpa: r.station_pressure_hpa,
  }));

  return (
    <div className="space-y-10">
      <div className="space-y-4">
        <p className="text-sm">
          <Link href={FLIGHTS} className="inline-flex min-h-11 items-center underline underline-offset-4">
            ← All flights
          </Link>
        </p>
        <PageHeader
          title={
            <>
              Flight · <LocalTime iso={flight.started_at} />
            </>
          }
          description={
            <StatusBadge
              status={flightTone(flight.status)}
              label={flightStatusLabel(flight.status)}
            />
          }
        />
      </div>

      {flight.error && (
        <ErrorState title="This flight did not finish cleanly" detail={flight.error} />
      )}

      {telemetry.length >= TELEMETRY_CAP && (
        <p
          role="status"
          className="rounded-lg border border-[var(--status-warning)] bg-[var(--surface)] p-3 text-sm"
        >
          <span aria-hidden="true">▲ </span>
          Showing the first {TELEMETRY_CAP.toLocaleString()} samples. This flight recorded
          more; the full record is in its CSV{flight.csv_path ? ` (${flight.csv_path})` : ""}.
        </p>
      )}

      <section aria-label="Summary" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <OwnerStat label="Flown by" who={flight} />
        <Stat label="Duration" value={flightDuration(flight.started_at, flight.ended_at)} />
        <Stat label="Samples" value={telemetry.length.toLocaleString()} />
        <Stat
          label="Ambient at launch"
          value={flight.ambient_start != null ? flight.ambient_start.toFixed(1) : null}
          unit={`°${unit}`}
        />
        <Stat
          label="Ground reference"
          value={flight.ground_z_m != null ? flight.ground_z_m.toFixed(3) : null}
          unit="m"
          hint="Lighthouse z captured at takeoff"
        />
      </section>

      <section aria-labelledby="findings-heading" className="space-y-3">
        <h2 id="findings-heading" className="text-lg font-semibold text-[var(--heading)]">
          What the pipeline found
        </h2>
        {!results.migrated || !findings.migrated ? (
          <p className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6 text-sm text-[var(--muted)]">
            Pipeline results are not on the web yet: the database has no pipeline tables. Apply
            migration 20261009000016_pipeline_results.sql; the laptop keeps every result and uploads
            it once they exist.
          </p>
        ) : !result ? (
          <p className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6 text-sm text-[var(--muted)]">
            This flight has not been processed. Process flights (DPP), at the top of the desktop
            app&apos;s Control page, was off when it flew — press Process this flight there; the result
            uploads here.
          </p>
        ) : findings.rows.length === 0 ? (
          <p className="rounded-lg border border-[var(--border)] bg-[var(--surface)] p-6 text-sm">
            Nothing in this flight departed from what was expected.
          </p>
        ) : (
          <FindingsList findings={findings.rows} excerpts={excerpts} />
        )}
        {result && flight.session_id && (
          <p className="text-sm">
            <Link href={processedPath(flight.session_id)} className="inline-flex min-h-11 items-center underline underline-offset-4">
              This session&apos;s processed data →
            </Link>
          </p>
        )}
      </section>

      {result && (
        <section aria-labelledby="processing-heading" className="space-y-3">
          <h2 id="processing-heading" className="text-lg font-semibold text-[var(--heading)]">
            Data processing
          </h2>
          <FlightProcessing result={result} telemetry={telemetry} findings={findings.rows} />
        </section>
      )}

      <section aria-labelledby="temp-heading" className="space-y-3">
        <h2 id="temp-heading" className="text-lg font-semibold text-[var(--heading)]">
          Temperature (°{unit})
        </h2>
        <p className="text-sm text-[var(--muted)]">
          The raw sensor reads warm because the drone heats it. Corrected is what the
          room actually was.
        </p>
        <TimeSeries
          rows={series}
          xKey="t"
          xLabel="Time (s)"
          xFormat="seconds"
          yLabel={`°${unit}`}
          series={[
            { key: "raw_temp", label: "Raw", slot: 1, unit: `°${unit}` },
            { key: "corrected_temp", label: "Corrected", slot: 2, unit: `°${unit}` },
            { key: "ambient_est", label: "Ambient estimate", slot: 3, unit: `°${unit}` },
          ]}
        />
      </section>

      {/* Different units, so different charts — never a second y-axis. */}
      <div className="grid gap-10 lg:grid-cols-2">
        <section aria-labelledby="alt-heading" className="space-y-3">
          <h2 id="alt-heading" className="text-lg font-semibold text-[var(--heading)]">
            Height above ground (m)
          </h2>
          <TimeSeries
            rows={series}
            xKey="t"
            xLabel="Time (s)"
            xFormat="seconds"
            yLabel="m"
            digits={3}
            series={[{ key: "height_m", label: "Height", slot: 1, unit: "m" }]}
          />
        </section>

        <section aria-labelledby="battery-heading" className="space-y-3">
          <h2 id="battery-heading" className="text-lg font-semibold text-[var(--heading)]">
            Battery (V)
          </h2>
          <TimeSeries
            rows={series}
            xKey="t"
            xLabel="Time (s)"
            xFormat="seconds"
            yLabel="V"
            series={[{ key: "battery_v", label: "Battery", slot: 1, unit: "V" }]}
          />
        </section>

        <section aria-labelledby="pressure-heading" className="space-y-3">
          <h2 id="pressure-heading" className="text-lg font-semibold text-[var(--heading)]">
            Station pressure (hPa)
          </h2>
          <TimeSeries
            rows={series}
            xKey="t"
            xLabel="Time (s)"
            xFormat="seconds"
            yLabel="hPa"
            series={[{ key: "station_pressure_hpa", label: "Pressure", slot: 1, unit: "hPa" }]}
          />
        </section>

        <section aria-labelledby="path-heading" className="space-y-3">
          <h2 id="path-heading" className="text-lg font-semibold text-[var(--heading)]">
            Flight path
          </h2>
          <FlightPath rows={telemetry} zones={zones} />
        </section>
      </div>

      {result && (
        <section aria-labelledby="errors-heading" className="space-y-3">
          <h2 id="errors-heading" className="text-lg font-semibold text-[var(--heading)]">
            What went wrong
          </h2>
          <p className="text-sm text-[var(--muted)]">
            Every anomaly and every reading the cleaner flagged, in time order — only the data each
            one is about is filled in. Show opens it in the readings below.
          </p>
          <ErrorTable marks={readingMarks} rows={telemetry} table="flight-readings" unit={unit} />
        </section>
      )}

      <section aria-labelledby="raw-heading" className="space-y-3">
        <h2 id="raw-heading" className="text-lg font-semibold text-[var(--heading)]">
          Raw readings
        </h2>
        <RawReadings rows={telemetry} unit={unit} marks={readingMarks} id="flight-readings" />
      </section>
    </div>
  );
}
