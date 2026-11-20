/**
 * The six numbers worth glancing at while flying.
 *
 * IT LIVES IN THE STRIP AT THE TOP, not in the console, and that is the point:
 * inside the Vitals tab these disappeared the moment the operator looked at the
 * camera or the scene. A battery reading that hides behind a tab is not a
 * battery reading. Up here they are on screen whatever the console is showing,
 * beside the keys they belong with.
 *
 * Moving them out also gave the command log its height back — it was sharing
 * the console with this grid, the tail and the attitude indicator.
 *
 * The rules come from ui.tsx unchanged: the label is muted, THE FIGURE NEVER
 * IS, a missing value says so rather than rendering blank or zero, and a tone
 * ships as icon + words.
 */

import type { Session, Telemetry } from "@/lib/agent";
import { formatNumber } from "@/lib/format";
import { StatusDot, type Tone } from "@/components/ui";
import { channelOf, useStationsHeard, type StationsHeard } from "@/lib/stationsHeard";

const read = (telemetry: Telemetry | null, name: string): number | null =>
  telemetry?.values[name] ?? null;

/** Position uncertainty in cm — the worst of the two horizontal variances. */
function uncertaintyCm(values: Record<string, number>): number | null {
  const worst = Math.max(values["kalman.varPX"] ?? 0, values["kalman.varPY"] ?? 0);
  return worst ? Math.sqrt(worst) * 100 : null;
}

export function VitalsNow({ telemetry, session }: {
  telemetry: Telemetry | null;
  session: Session | null;
}) {
  const vbat = read(telemetry, "pm.vbat") ?? session?.drone?.battery_v ?? null;
  const varCm = telemetry ? uncertaintyCm(telemetry.values) : null;
  const canfly = read(telemetry, "sys.canfly");
  const stations = useStationsHeard(telemetry);
  const battery = telemetry?.battery ?? null;

  return (
    <div className="mono grid shrink-0 grid-cols-2 gap-x-4 gap-y-2 text-xs xl:grid-cols-3">
      <Vital
        label={battery?.percent != null ? `Battery · ${battery.percent}%` : "Battery"}
        value={vbat} unit="V" digits={2}
        tone={vbat == null ? undefined
          : battery?.low || vbat < 3.3 ? "critical" : vbat < 3.75 ? "warning" : "good"}
      />
      <Vital label="Height" value={telemetry?.height_m ?? null} unit="m" digits={2} />
      <Vital
        label="Uncertainty" value={varCm} unit="cm" digits={1}
        tone={varCm == null ? undefined : varCm < 5 ? "good" : "warning"}
      />
      <Vital label="Thrust" value={read(telemetry, "stabilizer.thrust")} unit="" digits={0} />
      <Vital label="Temp, raw" value={read(telemetry, "baro.temp")} unit="°C" digits={1} />
      {/* The drone's own verdict, never a voltage threshold guessed here. */}
      <div className="min-w-0">
        <p className="text-[10px] uppercase tracking-[0.08em] text-[var(--muted)]">Will it arm</p>
        <p className="mt-0.5 font-semibold">
          {canfly == null ? (
            <span className="text-[var(--muted)]">—</span>
          ) : (
            <StatusDot tone={canfly ? "good" : "critical"}>{canfly ? "Yes" : "No"}</StatusDot>
          )}
        </p>
      </div>
      <StationsVital stations={stations} />
      {/* The agent's report, held for a few seconds so a sag under thrust is
          not a warning (sprint 3, task 19). It warns; it does not land. */}
      {battery?.low && (
        <p className="col-span-full font-semibold">
          <StatusDot tone="critical">
            {battery.motors_on
              ? `Battery under ${battery.flight_threshold_v.toFixed(2)} V under load`
              : `Battery under ${battery.threshold_percent}%`} — finish up and land soon.
          </StatusDot>
        </p>
      )}
    </div>
  );
}

/** Which base stations the drone hears now, by channel (stationsHeard.ts) —
 *  so "it flew on the barometer" is never a surprise after takeoff
 *  (2026-10-24: a manual flight heard none and nothing on screen said so). */
export function stationsWords(s: StationsHeard): { tone: Tone; text: string } {
  const named = s.heard.map(channelOf).join(" ");
  if (s.known.length === 0) {
    return s.light
      ? { tone: "warning", text: "Light, no station decoded" }
      : { tone: "critical", text: "None — no light on the deck" };
  }
  const count = `${s.heard.length} of ${s.known.length}`;
  // "0 of 2 heard" alone did not say which of two very different faults it
  // is (2026-10-24): no light at all, or light the drone cannot decode.
  if (s.heard.length === 0) {
    return { tone: "critical", text: s.light ? `${count} — light, not decoded` : `${count} — no light on the deck` };
  }
  return { tone: s.heard.length < s.known.length ? "warning" : "good", text: `${count} · ${named}` };
}

function StationsVital({ stations }: { stations: StationsHeard | null }) {
  const said = stations ? stationsWords(stations) : null;
  return (
    <div className="col-span-2 min-w-0">
      <p className="text-[10px] uppercase tracking-[0.08em] text-[var(--muted)]">Base stations</p>
      <p className="mt-0.5 font-semibold">
        {said ? <StatusDot tone={said.tone}>{said.text}</StatusDot> : <span className="text-[var(--muted)]">—</span>}
      </p>
    </div>
  );
}

function Vital({ label, value, unit, digits, tone }: {
  label: string;
  value: number | null;
  unit: string;
  digits: number;
  tone?: Tone;
}) {
  return (
    <div className="min-w-0">
      <p className="text-[10px] uppercase tracking-[0.08em] text-[var(--muted)]">{label}</p>
      <p className="mt-0.5 font-semibold">
        {value === null ? (
          <span className="text-[var(--muted)]">—</span>
        ) : tone ? (
          <StatusDot tone={tone}>{`${formatNumber(value, digits)}${unit && ` ${unit}`}`}</StatusDot>
        ) : (
          `${formatNumber(value, digits)}${unit && ` ${unit}`}`
        )}
      </p>
    </div>
  );
}
