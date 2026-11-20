"""ThermalEngine on the bench — sensors only, motors stationary (sprint 3, task 8).

Matthew's flight_sensor_test_with_recovery_and_geofencing.py tested the
temperature correction INSIDE a flight: take off, fly a serpentine scan at two
heights, land, with SensorLogger running alongside. To judge the sensor and
the correction on their own, this is the same ThermalEngine with the flight
taken out: the drone sits still, nothing arms, and every reading is recorded
next to a reference thermometer the operator types in.

    dronedeck thermal-test --minutes 10 --ambient 74F
    dronedeck thermal-test --minutes 30 --ambient 22C --reference 22.4C

What it gives:
  * a CSV (elapsed_s, raw_temp_c, corrected_temp_c, ...) — the input
    heat_bias.fit_cpu_warmup needs (task 7)
  * a summary: how far the corrected reading drifted in a room that did not
    change, and its error against the reference

A sample with any motor turning is NOT a bench sample: it is kept in the CSV
with motors_on=1, left out of the summary, and the summary says so.

STARTER CODE. Recorded at 2 Hz (Matthew's rate); not yet run on the drone.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from dronedeck.telemetry.correction import CorrectionResult, ThermalEngine

#: Matthew's sensor logging rate (LOG_PERIOD_MS = 500).
SAMPLE_PERIOD_S = 0.5
#: Minutes at each end compared for drift.
DRIFT_WINDOW_S = 60.0

COLUMNS = (
    "elapsed_s", "raw_temp_c", "corrected_temp_c", "ambient_est_c",
    "thermal_offset_c", "heat_bias_c", "deviation_c", "roc_c_per_s",
    "mode", "state", "event", "pressure_hpa", "thrust", "motors_on", "battery_v",
)


@dataclass(frozen=True)
class BenchSample:
    elapsed_s: float
    raw_temp_c: float
    pressure_hpa: float
    thrust: int = 0
    motor_max: int = 0          # largest of motor.m1..m4 — 0 when stationary
    battery_v: float | None = None

    @property
    def motors_on(self) -> bool:
        return self.thrust > 0 or self.motor_max > 0


@dataclass
class BenchSummary:
    samples: int
    motor_samples: int
    duration_s: float
    start_c: float              # mean corrected, first DRIFT_WINDOW_S
    end_c: float                # mean corrected, last DRIFT_WINDOW_S
    drift_c: float              # end - start: should be ~0 in a steady room
    reference_c: float | None
    mean_abs_error_c: float | None

    def lines(self) -> list[str]:
        out = [
            f"samples     {self.samples} over {self.duration_s / 60:.1f} min",
            f"corrected   {self.start_c:.2f} °C at the start, {self.end_c:.2f} °C at the end",
            f"drift       {self.drift_c:+.2f} °C",
        ]
        if self.mean_abs_error_c is not None and self.reference_c is not None:
            out.append(f"vs reference {self.reference_c:.2f} °C: "
                       f"mean abs error {self.mean_abs_error_c:.2f} °C")
        if self.motor_samples:
            out.append(f"WARNING     {self.motor_samples} samples had a motor turning — "
                       "left out; this is meant to be a motors-off test")
        return out


class ThermalBench:
    """Feeds samples through a ThermalEngine and writes each as a CSV row.
    No radio here: the CLI reads the drone and calls :meth:`add`."""

    def __init__(self, engine: ThermalEngine, out: IO[str] | None = None) -> None:
        self.engine = engine
        self._writer: Any = None
        if out is not None:
            self._writer = csv.DictWriter(out, fieldnames=COLUMNS)
            self._writer.writeheader()
        self.rows: list[dict[str, Any]] = []

    def add(self, s: BenchSample) -> CorrectionResult:
        r = self.engine.process(s.raw_temp_c, s.thrust, s.pressure_hpa)
        row = {
            "elapsed_s": round(s.elapsed_s, 3),
            "raw_temp_c": round(s.raw_temp_c, 4),
            "corrected_temp_c": round(r.corrected_temp_c, 4),
            "ambient_est_c": round(r.ambient_est_c, 4),
            "thermal_offset_c": round(r.thermal_offset_c, 4),
            "heat_bias_c": round(r.heat_bias_c, 4),
            "deviation_c": round(r.deviation_c, 4),
            "roc_c_per_s": round(r.roc_c_per_s, 5),
            "mode": str(r.mode), "state": str(r.state), "event": r.event,
            "pressure_hpa": round(s.pressure_hpa, 3),
            "thrust": s.thrust,
            "motors_on": int(s.motors_on),
            "battery_v": round(s.battery_v, 3) if s.battery_v is not None else "",
        }
        self.rows.append(row)
        if self._writer is not None:
            self._writer.writerow(row)
        return r

    def summary(self, reference_c: float | None = None) -> BenchSummary:
        return summarize(self.rows, reference_c)


def summarize(rows: Sequence[dict[str, Any]], reference_c: float | None = None) -> BenchSummary:
    still = [r for r in rows if not int(r["motors_on"])]
    if not still:
        raise ValueError("no motors-off samples to summarize")
    t0, t1 = float(still[0]["elapsed_s"]), float(still[-1]["elapsed_s"])
    window = min(DRIFT_WINDOW_S, max((t1 - t0) / 4, SAMPLE_PERIOD_S))
    head = [float(r["corrected_temp_c"]) for r in still if float(r["elapsed_s"]) <= t0 + window]
    tail = [float(r["corrected_temp_c"]) for r in still if float(r["elapsed_s"]) >= t1 - window]
    start, end = sum(head) / len(head), sum(tail) / len(tail)
    mae = None
    if reference_c is not None:
        mae = sum(abs(float(r["corrected_temp_c"]) - reference_c) for r in still) / len(still)
    return BenchSummary(
        samples=len(still), motor_samples=len(rows) - len(still), duration_s=t1 - t0,
        start_c=start, end_c=end, drift_c=end - start,
        reference_c=reference_c, mean_abs_error_c=mae,
    )


def default_path(root: Path, stamp: str) -> Path:
    folder = root / "thermal-bench"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"thermal-bench-{stamp}.csv"
