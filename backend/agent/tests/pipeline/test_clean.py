"""The cleaner (stage 1, story 4.2), robust@1: every fault flagged with the
right kind and column, and a real event left alone.

Rule by rule on small made-up points, then the ticket's acceptance on the one
real flight in the repo (the pipeline fixture) with faults planted into it by
ml/sensor-faults/plant_faults.py. The 66-flight comparison with hampel@1, which
robust@1 replaced, is in ml/sensor-faults/MEASUREMENTS.txt."""

from __future__ import annotations

import csv
import importlib.util
import math
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dronedeck.pipeline.contracts import (
    FlightContext,
    InspectionPoint,
    PointData,
    Reading,
)
from dronedeck.pipeline.sources import LocalFlightSource
from dronedeck.pipeline.stages.clean.robust import (
    BATTERY_STEP_V,
    STUCK_SAMPLES,
    RobustCleaner,
)

FIXTURE_CSV = (Path(__file__).parent / "fixtures" / "data" / "flights" / "2026-09-24"
               / "flight_372bbdc4_2026-09-24_04-18-20.csv")
SENSOR_FAULTS = Path(__file__).parents[4] / "ml" / "sensor-faults"
START = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)


def ctx(tmp_path: Path, unit: str = "C") -> FlightContext:
    return FlightContext(flight_id="f", session_id=None, temp_unit=unit,  # type: ignore[arg-type]
                         ground_z_m=None, started_at=START, workdir=tmp_path)


def point(columns: dict[str, list[float | None]], *, times: list[float] | None = None,
          indexes: list[int] | None = None, states: list[str] | None = None) -> PointData:
    """A point at 10 Hz, one reading per value. `times` and `indexes` override
    the regular spacing, to make a gap; `states` gives each row a
    thermal_state (the correction engine's)."""
    n = len(next(iter(columns.values())))
    times = times or [i * 0.1 for i in range(n)]
    indexes = indexes or list(range(n))
    states = states or ["FLIGHT (POWER)"] * n
    readings = tuple(
        Reading(index=indexes[i], recorded_at=START + timedelta(seconds=times[i]),
                t_s=times[i], values={c: v[i] for c, v in columns.items()},
                text={"thermal_state": states[i]})
        for i in range(n))
    return PointData(InspectionPoint("P1", None, 0.0, 0.0, 0.4), readings)


def noisy(n: int, base: float, wiggle: float) -> list[float | None]:
    """A signal with small, never-repeating noise, as the BMP388's is."""
    return [base + wiggle * math.sin(i * 1.7) for i in range(n)]


def flags_of(data: PointData, tmp_path: Path,
             unit: str = "C") -> set[tuple[int, str | None, str]]:
    result = RobustCleaner().clean(data, ctx(tmp_path, unit))
    return {(f.index, f.column, f.kind) for f in result.flags}


# ── each rule ────────────────────────────────────────────────────────────


def test_an_empty_point_is_an_empty_result(tmp_path):
    empty = PointData(InspectionPoint("P3", None, 0.0, 0.0, 0.4), ())
    result = RobustCleaner().clean(empty, ctx(tmp_path))
    assert result.readings == () and result.flags == ()


def test_a_clean_signal_gets_no_flags(tmp_path):
    data = point({"raw_temp": noisy(60, 34.2, 0.02),
                  "station_pressure_hpa": noisy(60, 1026.4, 0.01)})
    assert flags_of(data, tmp_path) == set()


def test_none_and_nan_are_missing(tmp_path):
    temps = noisy(30, 34.2, 0.02)
    temps[10], temps[20] = None, float("nan")
    assert flags_of(point({"raw_temp": temps}), tmp_path) == {
        (10, "raw_temp", "missing"), (20, "raw_temp", "missing")}


def test_a_value_the_sensor_cannot_measure_is_out_of_range(tmp_path):
    temps, pressure = noisy(30, 34.2, 0.02), noisy(30, 1026.4, 0.01)
    temps[5], pressure[15] = 150.0, 0.0
    assert flags_of(point({"raw_temp": temps, "station_pressure_hpa": pressure}),
                    tmp_path) == {(5, "raw_temp", "out_of_range"),
                                  (15, "station_pressure_hpa", "out_of_range")}


def test_a_one_sample_spike_is_flagged_and_says_by_how_much(tmp_path):
    temps = noisy(30, 22.0, 0.02)
    temps[15] = 25.0
    result = RobustCleaner().clean(point({"corrected_temp": temps}), ctx(tmp_path))
    (flag,) = result.flags
    assert (flag.index, flag.column, flag.kind) == (15, "corrected_temp", "spike")
    assert "25.00 °C" in flag.reason and "limit" in flag.reason


def test_a_held_step_is_not_a_spike(tmp_path):
    """The correction engine steps corrected_temp 4.6 °C at takeoff and holds it
    (row 27 of the fixture flight): a held change moves the median with it."""
    temps = [*noisy(30, 22.0, 0.005), *noisy(30, 26.65, 0.005)]
    assert flags_of(point({"corrected_temp": temps}), tmp_path) == set()


def test_a_slow_rise_like_a_hand_warmer_is_not_a_spike(tmp_path):
    temps = [22.0 + 0.05 * i + 0.01 * math.sin(i) for i in range(120)]   # +6 °C in 12 s
    assert flags_of(point({"raw_temp": temps, "corrected_temp": temps}), tmp_path) == set()


def test_a_value_held_too_long_is_stuck_and_so_is_what_is_computed_from_it(tmp_path):
    raw, corrected = noisy(40, 34.2, 0.02), noisy(40, 22.0, 0.02)
    raw[10:10 + STUCK_SAMPLES] = [34.21] * STUCK_SAMPLES
    stuck = set(range(10, 10 + STUCK_SAMPLES))
    assert flags_of(point({"raw_temp": raw, "corrected_temp": corrected}), tmp_path) == {
        *((i, "raw_temp", "stuck") for i in stuck),
        *((i, "corrected_temp", "stuck") for i in stuck)}


def test_a_value_held_just_under_the_limit_is_not_stuck(tmp_path):
    raw = noisy(40, 34.2, 0.02)
    raw[10:10 + STUCK_SAMPLES - 1] = [34.21] * (STUCK_SAMPLES - 1)
    assert flags_of(point({"raw_temp": raw}), tmp_path) == set()


def test_a_dropped_value_does_not_hide_a_stuck_run(tmp_path):
    """A frozen sensor that drops one value is still frozen: the NaN is missing,
    the run around it is stuck."""
    raw = noisy(40, 34.2, 0.02)
    run = range(5, 5 + 2 * STUCK_SAMPLES - 2)       # two halves, each under the limit
    for i in run:
        raw[i] = 34.21
    raw[5 + STUCK_SAMPLES - 1] = float("nan")
    assert flags_of(point({"raw_temp": raw}), tmp_path) == {
        (5 + STUCK_SAMPLES - 1, "raw_temp", "missing"),
        *((i, "raw_temp", "stuck") for i in run if i != 5 + STUCK_SAMPLES - 1)}


def test_a_missing_derived_value_is_missing_not_stuck(tmp_path):
    """corrected_temp's own missing flag wins over the stuck flag it inherits
    from raw_temp: a value keeps the first flag that fits."""
    raw, corrected = noisy(40, 34.2, 0.02), noisy(40, 22.0, 0.02)
    raw[10:10 + STUCK_SAMPLES] = [34.21] * STUCK_SAMPLES
    corrected[12] = None
    flags = flags_of(point({"raw_temp": raw, "corrected_temp": corrected}), tmp_path)
    assert (12, "corrected_temp", "missing") in flags
    assert (12, "corrected_temp", "stuck") not in flags
    assert (12, "raw_temp", "stuck") in flags


def test_the_battery_is_never_checked_for_stuck(tmp_path):
    """pm.vbat repeats for up to 28 readings in a real flight (MEASUREMENTS.txt)."""
    data = point({"raw_temp": noisy(40, 34.2, 0.02), "battery_v": [3.9] * 40})
    assert flags_of(data, tmp_path) == set()


def test_lost_time_is_a_gap_on_the_first_reading_after_it(tmp_path):
    times = [i * 0.1 for i in range(20)] + [2.5 + i * 0.1 for i in range(20)]
    data = point({"raw_temp": noisy(40, 34.2, 0.02)}, times=times)
    result = RobustCleaner().clean(data, ctx(tmp_path))
    (flag,) = result.flags
    assert (flag.index, flag.column, flag.kind) == (20, None, "gap")
    assert not result.usable(20, "raw_temp") and result.usable(19, "raw_temp")


def test_one_lost_row_is_a_gap_though_its_time_is_under_the_limit(tmp_path):
    indexes = [*range(20), *range(21, 41)]
    times = [i * 0.1 for i in indexes]               # 0.2 s apart: under 0.25 s
    data = point({"raw_temp": noisy(40, 34.2, 0.02)}, times=times, indexes=indexes)
    assert flags_of(data, tmp_path) == {(21, None, "gap")}


def test_a_column_the_flight_did_not_record_is_skipped(tmp_path):
    assert flags_of(point({"x_m": [0.0] * 30}), tmp_path) == set()


# ── corrected_temp: the correction engine's steps are not spikes ─────────


def test_the_engine_stepping_its_offset_when_the_state_switches_is_not_a_spike(tmp_path):
    """The cause of 294 of hampel@1's 332 false flags (MEASUREMENTS.txt): the
    engine scales its offset per thermal_state, so corrected_temp steps for a
    few rows each time the state flips — while raw_temp, the sensor, is calm."""
    raw = noisy(60, 34.2, 0.01)
    states = (["FLIGHT (POWER)"] * 20 + ["FLIGHT (COOLING)"] * 4 + ["FLIGHT (POWER)"] * 16
              + ["FLIGHT (COOLING)"] * 3 + ["FLIGHT (POWER)"] * 17)
    offset = {"FLIGHT (POWER)": 9.9, "FLIGHT (COOLING)": 7.6}
    corrected = [r - offset[s] for r, s in zip(raw, states, strict=True)]
    data = point({"raw_temp": raw, "corrected_temp": corrected}, states=states)
    assert flags_of(data, tmp_path) == set()


def test_a_spike_in_the_correction_alone_is_flagged_on_corrected_temp(tmp_path):
    raw = noisy(40, 34.2, 0.01)
    corrected = [r - 9.9 for r in raw]
    corrected[20] += 2.5
    result = RobustCleaner().clean(point({"raw_temp": raw, "corrected_temp": corrected}),
                                   ctx(tmp_path))
    (flag,) = result.flags
    assert (flag.index, flag.column, flag.kind) == (20, "corrected_temp", "spike")
    assert "offset" in flag.reason


def test_a_raw_spike_is_a_spike_in_what_is_computed_from_it_too(tmp_path):
    raw = noisy(40, 34.2, 0.01)
    corrected = [r - 9.9 for r in raw]
    raw[20] += 3.0
    corrected[20] += 3.0
    assert flags_of(point({"raw_temp": raw, "corrected_temp": corrected}), tmp_path) == {
        (20, "raw_temp", "spike"), (20, "corrected_temp", "spike")}


def test_a_raw_value_out_of_range_never_makes_corrected_temp_a_spike(tmp_path):
    """150 °C in raw_temp is out_of_range there; corrected_temp beside it is a
    different, plausible number and is not judged by the bad one."""
    raw = noisy(40, 34.2, 0.01)
    corrected = [r - 9.9 for r in raw]
    raw[20] = 150.0
    assert flags_of(point({"raw_temp": raw, "corrected_temp": corrected}), tmp_path) == {
        (20, "raw_temp", "out_of_range")}


def test_a_value_frozen_across_lost_time_is_still_stuck(tmp_path):
    """hampel@1 reset its count at every gap and missed 23 planted runs."""
    raw = noisy(40, 34.2, 0.02)
    run = range(10, 10 + STUCK_SAMPLES + 2)
    for i in run:
        raw[i] = 34.21
    indexes = [*range(15), *range(16, 41)]          # one row lost inside the run
    times = [i * 0.1 for i in indexes]
    flags = flags_of(point({"raw_temp": raw}, times=times, indexes=indexes), tmp_path)
    assert {(indexes[i], "raw_temp", "stuck") for i in run} <= flags


# ── position ─────────────────────────────────────────────────────────────


def positions(n: int, received: list[int] | None = None,
              x: list[float] | None = None) -> dict[str, list[float | None]]:
    return {"x_m": list(x or [0.5 + 0.001 * i for i in range(n)]),
            "y_m": [0.2] * n, "z_m": [0.4] * n,
            "lighthouse_received": [float(v) for v in (received or [1] * n)]}


def test_a_position_with_no_base_station_is_untrusted(tmp_path):
    received = [1] * 20
    received[5:8] = [0, 0, 0]
    flags = flags_of(point(positions(20, received)), tmp_path)
    assert flags == {(i, c, "untrusted") for i in (5, 6, 7) for c in ("x_m", "y_m", "z_m")}


def test_a_position_jumping_faster_than_a_drone_flies_is_implausible(tmp_path):
    x = [0.5] * 20
    x[10] = 1.5                                     # 1 m in 0.1 s: 10 m/s
    flags = flags_of(point(positions(20, x=x)), tmp_path)
    assert flags == {(10, c, "implausible") for c in ("x_m", "y_m", "z_m")}


def test_after_a_jump_the_estimate_that_stays_somewhere_new_becomes_the_reference(tmp_path):
    """An estimator that re-locks elsewhere is flagged for half a second, then
    believed — never flagged for the rest of the flight."""
    x = [0.5] * 10 + [1.5] * 30
    flags = flags_of(point(positions(40, x=x)), tmp_path)
    jumped = {i for i, _, kind in flags if kind == "implausible"}
    assert jumped and max(jumped) < 10 + 6


def test_a_position_beyond_any_base_station_is_out_of_range(tmp_path):
    x = [0.5] * 20
    x[4] = 91.0
    assert (4, "x_m", "out_of_range") in flags_of(point(positions(20, x=x)), tmp_path)


# ── battery ──────────────────────────────────────────────────────────────


def test_a_battery_voltage_no_cell_gives_is_out_of_range(tmp_path):
    volts = [3.9] * 20
    volts[7] = 0.0
    assert flags_of(point({"battery_v": volts}), tmp_path) == {(7, "battery_v", "out_of_range")}


def test_a_battery_jump_no_load_step_makes_is_implausible_and_a_sag_is_not(tmp_path):
    volts = [3.9] * 30
    volts[10] = 3.9 - 0.78                          # the largest real sag: fine
    volts[20] = 3.9 - BATTERY_STEP_V - 0.1
    assert flags_of(point({"battery_v": volts}), tmp_path) == {(20, "battery_v", "implausible")}


def test_fahrenheit_limits_are_converted(tmp_path):
    """150 °F is 65.6 °C, a real reading; 200 °F is 93 °C, past the BMP388's 85 °C."""
    temps = noisy(30, 92.0, 0.04)
    temps[10], temps[20] = 150.0, 200.0
    flags = flags_of(point({"raw_temp": temps}), tmp_path, unit="F")
    assert (20, "raw_temp", "out_of_range") in flags
    assert all(f[0] != 10 or f[2] != "out_of_range" for f in flags)


# ── the real flight ──────────────────────────────────────────────────────


def whole_flight(tmp_path: Path) -> PointData:
    """The fixture flight as ONE point (no session): all 400 readings, including
    the takeoff step at row 27 that falls outside P1 and P2."""
    folder = tmp_path / "flights" / "2026-09-24"
    folder.mkdir(parents=True)
    shutil.copy(FIXTURE_CSV, folder)
    (data,) = LocalFlightSource(tmp_path).load("372bbdc4-d323-42bb-9e6c-29ef02e3794c").points
    return data


def test_the_real_flight_gets_no_flags_on_its_sensors(tmp_path):
    """No sensor or battery flag on the real flight. Its positions are all
    untrusted, which is true: no base station was received in that flight
    (lighthouse_received 0 from row 2 on, MEASUREMENTS.txt)."""
    data = whole_flight(tmp_path)
    assert len(data.readings) == 400
    flags = flags_of(data, tmp_path)
    assert {f for f in flags if f[1] not in ("x_m", "y_m", "z_m")} == set()
    assert {kind for _, column, kind in flags if column in ("x_m", "y_m", "z_m")} == {"untrusted"}


def load_evaluate():
    spec = importlib.util.spec_from_file_location("evaluate", SENSOR_FAULTS / "evaluate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # @dataclass looks its module up here
    spec.loader.exec_module(module)
    return module


def fixture_rows(unit: str = "C") -> list[dict[str, str]]:
    with FIXTURE_CSV.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if unit == "F":
        for row in rows:
            row["temp_unit"] = "F"
            for c in ("raw_temp", "corrected_temp", "expected_raw", "ambient_est"):
                row[c] = repr(float(row[c]) * 9 / 5 + 32)
            for c in ("deviation", "thermal_offset", "roc_per_s"):    # differences
                row[c] = repr(float(row[c]) * 9 / 5)
    return rows


@pytest.mark.parametrize("seed", range(5))
def test_acceptance_on_the_real_flight_with_planted_faults(seed):
    """The ticket's ACCEPTANCE, on the one real flight in the repo: every planted
    fault caught with the right kind and column, at most 1 % of the rest
    flagged, and the hand-warmer ramp untouched."""
    score = load_evaluate().evaluate(fixture_rows(), seed)
    assert score.missed == ()
    assert score.caught == score.planted > 0
    assert score.false_rate <= 0.01, score.false_flags
    assert score.ramp_flags == ()


def test_a_fahrenheit_flight_gets_the_same_flags_as_the_same_flight_in_celsius():
    evaluate = load_evaluate().evaluate
    celsius, fahrenheit = evaluate(fixture_rows("C"), 3), evaluate(fixture_rows("F"), 3)
    assert {(f.index, f.column, f.kind) for f in fahrenheit.flags} == \
        {(f.index, f.column, f.kind) for f in celsius.flags}
    assert fahrenheit.missed == ()
