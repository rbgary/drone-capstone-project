"""Thermal correction engine.

These pin the behaviour the tuned constants were chosen to produce. If a change
to the constants breaks one of these, that is the test doing its job — the
numbers came from flights against a reference thermometer, not from theory.
"""

from __future__ import annotations

import math

import pytest

from dronedeck.telemetry import correction
from dronedeck.telemetry.correction import (
    CorrectionTuning,
    Mode,
    ThermalEngine,
    ThermalState,
    air_density,
    c_to_f,
    f_to_c,
)

AMBIENT_C = 22.0
# The board reads high at rest; 3 °C of self-heating is typical.
IDLE_RAW_C = 25.0
PRESSURE_HPA = 1013.25


def feed(engine: ThermalEngine, raw_c: float, thrust: int = 0, n: int = 1):
    result = None
    for _ in range(n):
        result = engine.process(raw_c, thrust, PRESSURE_HPA)
    return result


def test_corrected_temperature_starts_at_ambient():
    """At startup the offset is measured, so corrected should equal ambient."""
    engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
    result = feed(engine, IDLE_RAW_C)
    assert result.corrected_temp_c == pytest.approx(AMBIENT_C, abs=0.01)
    assert result.thermal_offset_c == pytest.approx(3.0, abs=0.01)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(correction.time, "monotonic", c)
    return c


def test_flight_scales_the_offset_down(clock):
    """Airflow cools the board, so less of the raw reading is self-heating."""
    engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
    idle = feed(engine, IDLE_RAW_C, thrust=0)

    engine_flight = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
    feed(engine_flight, IDLE_RAW_C, thrust=40000)
    clock.now += 120.0  # long past the cooling ramp
    flying = feed(engine_flight, IDLE_RAW_C, thrust=40000)

    # Same raw reading, but in flight less is attributed to self-heating,
    # so the corrected value is higher.
    assert flying.corrected_temp_c > idle.corrected_temp_c
    assert flying.state is ThermalState.FLIGHT_POWER
    assert flying.corrected_temp_c == pytest.approx(
        IDLE_RAW_C - 3.0 * CorrectionTuning().scale_power, abs=0.01)


class TestTakeoffCoolingRamp:
    """The board cools over tens of seconds after takeoff, not at once
    (flights 7148a651 and fca56de0, 2026-11-07, read 2-4.5 °F warm early)."""

    def corrected_at(self, clock, engine, secs: float) -> float:
        clock.now += secs
        return feed(engine, IDLE_RAW_C, thrust=40000).corrected_temp_c

    def test_takeoff_starts_from_the_ground_offset(self, clock):
        engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
        first = feed(engine, IDLE_RAW_C, thrust=40000)
        # At the moment of takeoff the whole ground offset is still there.
        assert first.corrected_temp_c == pytest.approx(AMBIENT_C, abs=0.01)

    def test_scale_reaches_63_percent_at_one_time_constant(self, clock):
        tuning = CorrectionTuning()
        engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
        feed(engine, IDLE_RAW_C, thrust=40000)
        got = self.corrected_at(clock, engine, tuning.flight_cooling_tau_s)
        scale = 1.0 + (tuning.scale_power - 1.0) * (1.0 - math.exp(-1.0))
        assert got == pytest.approx(IDLE_RAW_C - 3.0 * scale, abs=0.01)

    def test_corrected_rises_steadily_during_the_ramp(self, clock):
        engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
        feed(engine, IDLE_RAW_C, thrust=40000)
        readings = [self.corrected_at(clock, engine, 5.0) for _ in range(12)]
        assert readings == sorted(readings)
        assert readings[-1] - readings[0] > 0.3

    def test_landing_restarts_the_ramp(self, clock):
        engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
        feed(engine, IDLE_RAW_C, thrust=40000)
        self.corrected_at(clock, engine, 120.0)
        feed(engine, IDLE_RAW_C, thrust=0)
        clock.now += 1.0
        again = feed(engine, IDLE_RAW_C, thrust=40000)
        assert again.corrected_temp_c == pytest.approx(AMBIENT_C, abs=0.05)

    def test_zero_time_constant_is_the_old_instant_step(self, clock):
        engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C, CorrectionTuning(flight_cooling_tau_s=0.0))
        first = feed(engine, IDLE_RAW_C, thrust=40000)
        assert first.corrected_temp_c == pytest.approx(
            IDLE_RAW_C - 3.0 * CorrectionTuning().scale_power, abs=0.01)


def test_thrust_bands_select_different_scaling():
    engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
    assert feed(engine, IDLE_RAW_C, thrust=0).state is ThermalState.IDLE
    assert feed(engine, IDLE_RAW_C, thrust=20000).state is ThermalState.FLIGHT_COOLING
    assert feed(engine, IDLE_RAW_C, thrust=40000).state is ThermalState.FLIGHT_POWER


def test_estimates_are_frozen_while_flying():
    """In flight the thermal model is unreliable, so ambient must not move."""
    engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
    before = engine.ambient_est_c
    feed(engine, IDLE_RAW_C - 5.0, thrust=40000, n=50)
    assert engine.ambient_est_c == pytest.approx(before, abs=1e-9)


def test_sustained_cold_triggers_transition():
    """A real environment change should be detected — but only after it sustains."""
    engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
    assert engine.mode is Mode.STABLE

    # One cold sample is noise, not a room change.
    feed(engine, 15.0, thrust=0, n=1)
    assert engine.mode is Mode.STABLE

    # Sustained over env_sustain_samples, it is a room change.
    feed(engine, 15.0, thrust=0, n=6)
    assert engine.mode is Mode.TRANSITION


def test_transition_waits_before_rebasing():
    """The original failure mode: re-basing ambient before the trend settles
    produced an unrealistic corrected temperature when moving cold -> warm."""
    engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
    feed(engine, 15.0, thrust=0, n=6)
    assert engine.mode is Mode.TRANSITION

    ambient_on_entry = engine.ambient_est_c
    # A single settled sample must not be enough to move ambient.
    feed(engine, 15.0, thrust=0, n=1)
    assert engine.ambient_est_c == pytest.approx(ambient_on_entry, abs=1e-9)


def test_ambient_step_is_capped():
    """One wild sample must not lurch the ambient estimate."""
    engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
    start = engine.ambient_est_c
    feed(engine, -40.0, thrust=0, n=40)
    moved = abs(engine.ambient_est_c - start)
    # Many steps are allowed, but each is capped; it cannot jump 60 degrees.
    assert moved < 30.0


def test_stable_readings_keep_mode_stable():
    engine = ThermalEngine(IDLE_RAW_C, AMBIENT_C)
    feed(engine, IDLE_RAW_C, thrust=0, n=30)
    assert engine.mode is Mode.STABLE


class TestDerivedQuantities:
    def test_air_density_at_sea_level(self):
        # ISA sea level, 15 °C -> about 1.225 kg/m3.
        assert air_density(1013.25, 15.0) == pytest.approx(1.225, abs=0.005)

    def test_density_falls_as_temperature_rises(self):
        assert air_density(1013.25, 35.0) < air_density(1013.25, 15.0)

    def test_temperature_conversions_round_trip(self):
        for c in (-40.0, 0.0, 22.0, 100.0):
            assert f_to_c(c_to_f(c)) == pytest.approx(c, abs=1e-9)

    def test_known_conversion(self):
        assert c_to_f(22.0) == pytest.approx(71.6, abs=0.01)
        assert f_to_c(74.0) == pytest.approx(23.333, abs=0.01)
