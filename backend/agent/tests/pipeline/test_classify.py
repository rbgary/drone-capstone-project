"""The classifier (stage 3, story 4.6), blocks@1: blocks of readings that depart
from what was expected, found where they are and nowhere else.

Synthetic flights that behave like the real drone: raw_temp cooling in the
propellers' air from takeoff (MEASUREMENTS.txt: 34.7 → 29.6 °C in 48 s),
pressure falling 0.12 hPa per metre climbed. The calibration on the lab's
real flights is ml/anomaly-eval/."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from dronedeck.pipeline.contracts import (
    CleanResult,
    FlightContext,
    InspectionPoint,
    PointData,
    Reading,
    ReadingFlag,
)
from dronedeck.pipeline.stages.classify.blocks import (
    SETTLE_S,
    BlocksClassifier,
    G,
    fit_cooling,
)
from dronedeck.pipeline.stages.classify.pelt import pelt

START = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
RHO = 1.2


def flight(seconds: float = 60.0, *, bump_c: float = 0.0, bump_at_s: float = 30.0,
           step_hpa: float = 0.0, step_at_s: float = 30.0, climb_m: float = 0.0,
           unit: str = "C", positioned: bool = True, seed: int = 0) -> PointData:
    """A flight at 10 Hz: 1 s on the ground, then airborne. raw_temp cools from
    34.6 toward 29 °C (τ 40 s); pressure 1016 hPa minus ρgz; optionally a bump
    of warm air (rise 5 s, hold 5 s, fall 5 s), a pressure step held 5 s, or a
    climb of `climb_m` over the middle third."""
    rng = np.random.default_rng(seed)
    readings = []
    n = int(seconds * 10)
    for i in range(n):
        t = i * 0.1
        airborne = t >= 1.0
        temp = 29.0 + 5.6 * math.exp(-max(t - 1.0, 0.0) / 40.0) + rng.normal(0, 0.02)
        u = t - bump_at_s
        lift = 0.0 if u < 0 else min(u / 5, 1.0) if u < 10 else max(1 - (u - 10) / 5, 0.0)
        temp += bump_c * lift
        z = 0.0 if not airborne else 0.4 + climb_m * min(max((t - seconds / 3) / (seconds / 3),
                                                               0.0), 1.0)
        pressure = 1016.0 - RHO * G * z / 100.0 + rng.normal(0, 0.015)
        if step_at_s <= t < step_at_s + 5:
            pressure += step_hpa
        raw = temp * 9 / 5 + 32 if unit == "F" else temp
        readings.append(Reading(
            index=i, recorded_at=START + timedelta(seconds=t), t_s=t,
            values={"raw_temp": raw, "corrected_temp": raw - 10.0,
                    "station_pressure_hpa": pressure, "air_density_kg_m3": RHO,
                    "x_m": 0.5, "y_m": 0.2, "z_m": z,
                    "lighthouse_received": 1.0 if positioned else 0.0},
            point_id="P1" if 25 <= t < 45 else None,
            text={"thermal_state": "FLIGHT (POWER)" if airborne else "IDLE"}))
    return PointData(InspectionPoint("flight", None, 0, 0, 0), tuple(readings))


def ctx(tmp_path: Path, unit: str = "C") -> FlightContext:
    return FlightContext("f", None, unit, None, START, tmp_path,  # type: ignore[arg-type]
                         points=(InspectionPoint("P1", None, 0.5, 0.2, 0.4),))


def classify(data: PointData, tmp_path: Path, unit: str = "C",
             flags: tuple[ReadingFlag, ...] = ()):
    return BlocksClassifier().classify(data, CleanResult(data.readings, flags),
                                       ctx(tmp_path, unit))


def test_a_normal_flight_cools_as_expected_and_raises_nothing(tmp_path):
    result = classify(flight(), tmp_path)
    assert result.events == ()
    assert result.sensors.value == "normal"
    temperature, pressure = result.tracks
    assert temperature.model == "cooling-curve" and pressure.model == "linear"
    residual = np.array(temperature.observed) - np.array(temperature.expected)
    assert np.abs(residual).max() < 0.15                  # the curve follows the cooling
    assert result.features["temp_tau_s"] == pytest.approx(40, rel=0.5)


def test_takeoff_is_left_out(tmp_path):
    (temperature, _) = classify(flight(), tmp_path).tracks
    first = temperature.indexes[0]
    assert first * 0.1 >= 1.0 + SETTLE_S - 0.05


def test_warm_air_is_one_rise_where_it_was(tmp_path):
    result = classify(flight(bump_c=3.0, bump_at_s=30.0), tmp_path)
    (event,) = result.events
    assert (event.signal, event.direction) == ("temperature", "rise")
    assert 28 <= event.t_start_s <= 36 and 38 <= event.t_end_s <= 46
    assert event.delta > 1.5 and event.z > 4
    assert event.point_ids == ("P1",)
    assert (event.x_m, event.y_m) == (0.5, 0.2)
    assert result.sensors.value == "faulty"


def test_a_pressure_step_is_found_and_a_climb_is_not(tmp_path):
    (event,) = classify(flight(step_hpa=0.5, step_at_s=30.0), tmp_path).events
    assert (event.signal, event.direction) == ("pressure", "rise")
    assert event.delta == pytest.approx(0.5, abs=0.15)
    # A 0.6 m climb moves the barometer 0.07 hPa: physics, not an event.
    assert classify(flight(climb_m=0.6), tmp_path).events == ()


def test_without_a_measured_height_the_pressure_track_says_so(tmp_path):
    data = flight(positioned=False)
    untrusted = tuple(ReadingFlag(r.index, "z_m", "untrusted", "no base station")
                      for r in data.readings)
    (_, pressure) = classify(data, tmp_path, flags=untrusted).tracks
    assert pressure.model == "linear, height not measured"


def test_a_flagged_value_never_reaches_a_model(tmp_path):
    data = flight(bump_c=3.0)
    spiked = list(data.readings)
    spiked[300] = replace(spiked[300], values={**spiked[300].values, "raw_temp": 80.0})
    flag = (ReadingFlag(300, "raw_temp", "spike", "planted"),)
    clean_run = classify(PointData(data.point, tuple(spiked)), tmp_path, flags=flag)
    without = classify(data, tmp_path, flags=flag)
    assert clean_run.events == without.events
    assert 300 not in clean_run.tracks[0].indexes


def test_a_fahrenheit_flight_gives_the_same_events_in_fahrenheit(tmp_path):
    c = classify(flight(bump_c=3.0), tmp_path)
    f = classify(flight(bump_c=3.0, unit="F"), tmp_path, unit="F")
    assert [(e.start_index, e.end_index) for e in f.events] == \
        [(e.start_index, e.end_index) for e in c.events]
    assert f.events[0].delta == pytest.approx(c.events[0].delta * 9 / 5, rel=1e-3)
    assert f.events[0].observed == pytest.approx(c.events[0].observed * 9 / 5 + 32, rel=1e-3)
    assert f.tracks[0].unit == "F"


def test_too_short_a_flight_is_unknown_and_says_why(tmp_path):
    result = classify(flight(seconds=4.0), tmp_path)
    assert result.sensors.value == "unknown"
    assert result.sensors.reason and "too few" in result.sensors.reason
    assert result.tracks == ()


def test_an_empty_flight_is_unknown(tmp_path):
    empty = PointData(InspectionPoint("flight", None, 0, 0, 0), ())
    result = classify(empty, tmp_path)
    assert result.sensors.value == "unknown"


def test_the_cooling_fit_holds_against_an_outlier_burst():
    t = np.arange(0, 60, 0.1)
    y = 29 + 5.6 * np.exp(-t / 40)
    y[200:230] += 4.0                                      # warm air for 3 s
    fit = fit_cooling(t, y)
    residual = y - fit.expected
    assert np.median(np.abs(residual[:200])) < 0.1         # the curve did not bend to it
    assert residual[200:230].mean() > 3.0


def test_pelt_agrees_with_ruptures():
    ruptures = pytest.importorskip("ruptures")
    rng = np.random.default_rng(1)
    for _ in range(100):
        n, m = int(rng.integers(40, 300)), int(rng.integers(2, 25))
        y = rng.normal(0, 1, n)
        for _ in range(int(rng.integers(0, 5))):
            y[int(rng.integers(0, n)):] += rng.normal(0, 3)
        penalty = float(rng.uniform(2, 30))
        expected = ruptures.Pelt(model="l2", min_size=m, jump=1).fit(y).predict(pen=penalty)
        assert pelt(y, penalty, m) == expected


def test_pelt_on_too_little_is_one_block():
    assert pelt(np.zeros(5), 1.0, 10) == [5]
    assert pelt(np.zeros(0), 1.0, 10) == []
