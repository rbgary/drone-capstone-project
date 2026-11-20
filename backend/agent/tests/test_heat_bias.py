"""CPU and thrust heat-bias correction (sprint 3, task 7)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from dronedeck.telemetry import correction
from dronedeck.telemetry.correction import ThermalEngine
from dronedeck.telemetry.heat_bias import (
    BENCH_CPU_WARMUP,
    HeatBiasModel,
    HeatBiasTuning,
    fit_cpu_warmup,
    fit_cpu_warmup_csv,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    c = FakeClock()
    monkeypatch.setattr(correction.time, "monotonic", c)
    return c


ROOM_C = 22.0
STARTUP_OFFSET_C = 3.0
AMP_C, TAU_S = 1.5, 240.0


def warming_raw(t: float) -> float:
    """A drone on the bench in a steady room: the board keeps warming."""
    return ROOM_C + STARTUP_OFFSET_C + AMP_C * (1 - math.exp(-t / TAU_S))


class TestModel:
    def test_default_changes_nothing(self):
        m = HeatBiasModel()
        assert not m.tuning.enabled
        assert m.update(0.0, 0) == 0.0
        assert m.update(600.0, 60000) == 0.0

    def test_cpu_bias_rises_to_its_amplitude(self):
        m = HeatBiasModel(HeatBiasTuning(cpu_amp_c=AMP_C, cpu_tau_s=TAU_S))
        assert m.cpu_bias_c(0) == 0.0
        assert m.cpu_bias_c(TAU_S) == pytest.approx(AMP_C * (1 - math.exp(-1)))
        assert m.cpu_bias_c(10 * TAU_S) == pytest.approx(AMP_C, abs=1e-3)

    def test_thrust_heat_lags_and_decays(self):
        m = HeatBiasModel(HeatBiasTuning(thrust_gain_c=2.0, thrust_tau_s=10.0))
        m.update(0.0, 0)
        for s in range(1, 101):                  # 100 s at full thrust
            m.update(float(s), 65535)
        assert m.thrust_heat_c == pytest.approx(2.0, abs=0.01)
        m.update(101.0, 0)
        assert 0 < m.thrust_heat_c < 2.0         # it fades, it does not switch off
        for s in range(102, 200):
            m.update(float(s), 0)
        assert m.thrust_heat_c == pytest.approx(0.0, abs=0.01)


class TestFit:
    def test_recovers_the_warm_up_curve(self):
        ts = [float(t) for t in range(0, 1200, 2)]
        fit = fit_cpu_warmup(ts, [warming_raw(t) for t in ts])
        assert fit.cpu_amp_c == pytest.approx(AMP_C, rel=0.05)
        assert fit.cpu_tau_s == pytest.approx(TAU_S, rel=0.1)
        assert fit.rms_error_c < 0.05

    def test_too_few_samples_is_refused(self):
        with pytest.raises(ValueError):
            fit_cpu_warmup([0.0, 1.0], [20.0, 20.1])

    def test_fits_from_a_thermal_test_csv(self, tmp_path: Path):
        p = tmp_path / "bench.csv"
        rows = ["elapsed_s,raw_temp_c,thrust"]
        rows += [f"{t},{warming_raw(t):.4f},0" for t in range(0, 1200, 2)]
        p.write_text("\n".join(rows) + "\n")
        fit = fit_cpu_warmup_csv(p)
        assert fit.cpu_amp_c == pytest.approx(AMP_C, rel=0.05)


class TestEngine:
    def run(self, clock: FakeClock, engine: ThermalEngine, secs: int) -> list[float]:
        out = []
        start = clock.now
        for t in range(1, secs + 1):
            clock.now = start + t
            out.append(engine.process(warming_raw(t), 0, 1013.25).corrected_temp_c)
        return out

    def test_without_a_model_the_engine_is_unchanged(self, clock):
        engine = ThermalEngine(warming_raw(0), ROOM_C)
        r = engine.process(warming_raw(0), 0, 1013.25)
        assert r.heat_bias_c == 0.0

    def test_the_fitted_model_keeps_a_steady_room_steady(self, clock):
        """The demo: in a room that does not change, the corrected reading
        drifts less with the CPU warm-up removed than without."""
        plain = self.run(clock, ThermalEngine(warming_raw(0), ROOM_C), 900)
        clock.now = 1000.0
        model = HeatBiasModel(HeatBiasTuning(cpu_amp_c=AMP_C, cpu_tau_s=TAU_S))
        fixed = self.run(clock, ThermalEngine(warming_raw(0), ROOM_C, heat_bias=model), 900)
        plain_err = abs(plain[-1] - ROOM_C)
        fixed_err = abs(fixed[-1] - ROOM_C)
        assert fixed_err < plain_err
        assert fixed_err < 0.3

    def test_the_bench_fit_keeps_a_lab_like_warm_up_flat(self, clock):
        """BENCH_CPU_WARMUP (the 2026-11-07 bench run): a board warming the
        way the lab drone's did reads the room for five minutes."""
        a, tau = BENCH_CPU_WARMUP.cpu_amp_c, BENCH_CPU_WARMUP.cpu_tau_s

        def lab_raw(t: float) -> float:
            return ROOM_C + STARTUP_OFFSET_C + a * (1 - math.exp(-t / tau))

        engine = ThermalEngine(lab_raw(0), ROOM_C, heat_bias=HeatBiasModel(BENCH_CPU_WARMUP))
        start = clock.now
        out = []
        for t in range(1, 301):
            clock.now = start + t
            out.append(engine.process(lab_raw(t), 0, 1013.25).corrected_temp_c)
        # The EMA lags the fast first minute a little; the mean stays on the room.
        assert max(abs(c - ROOM_C) for c in out) < 0.25
        assert sum(abs(c - ROOM_C) for c in out) / len(out) < 0.1
