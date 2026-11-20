"""The sensor-only ThermalEngine test (sprint 3, task 8)."""

from __future__ import annotations

import io
import math
from pathlib import Path

import pytest

from dronedeck.cli import build_parser
from dronedeck.telemetry import correction
from dronedeck.telemetry.bench import BenchSample, ThermalBench, summarize
from dronedeck.telemetry.correction import ThermalEngine
from dronedeck.telemetry.heat_bias import fit_cpu_warmup_csv


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(correction.time, "monotonic", lambda: now[0])
    return now


def raw_at(t: float) -> float:
    return 25.0 + 1.2 * (1 - math.exp(-t / 200.0))   # 22 °C room, board warming


def record(clock, secs: int, *, motors_from: int | None = None) -> tuple[ThermalBench, io.StringIO]:
    out = io.StringIO()
    bench = ThermalBench(ThermalEngine(raw_at(0), 22.0), out)
    for i in range(secs * 2):
        t = i * 0.5
        clock[0] = 1000.0 + t
        on = motors_from is not None and t >= motors_from
        bench.add(BenchSample(t, raw_at(t), 1013.25, thrust=30000 if on else 0,
                              motor_max=30000 if on else 0, battery_v=4.0))
    return bench, out


class TestBench:
    def test_every_sample_is_a_row(self, clock):
        bench, _ = record(clock, 60)
        assert len(bench.rows) == 120
        assert bench.rows[0]["motors_on"] == 0

    def test_summary_reports_drift_and_error(self, clock):
        s = record(clock, 600)[0].summary(reference_c=22.0)
        assert s.samples == 1200
        assert s.motor_samples == 0
        assert s.drift_c > 0             # the uncorrected warm-up shows as drift
        assert s.mean_abs_error_c is not None
        assert any("drift" in line for line in s.lines())

    def test_motor_samples_are_left_out_and_said(self, clock):
        s = record(clock, 120, motors_from=60)[0].summary()
        assert s.motor_samples == 120
        assert any("WARNING" in line for line in s.lines())

    def test_nothing_to_summarize_is_an_error(self):
        with pytest.raises(ValueError):
            summarize([{"motors_on": 1, "elapsed_s": 0, "corrected_temp_c": 22}])

    def test_its_csv_is_what_the_heat_bias_fit_reads(self, clock, tmp_path: Path):
        _, out = record(clock, 900)
        p = tmp_path / "bench.csv"
        p.write_text(out.getvalue())
        fit = fit_cpu_warmup_csv(p)
        assert fit.cpu_amp_c == pytest.approx(1.2, rel=0.1)


def test_the_command_parses():
    args = build_parser().parse_args(
        ["thermal-test", "--minutes", "5", "--ambient", "74F", "--reference", "74.2F"])
    assert (args.command, args.minutes, args.ambient) == ("thermal-test", 5.0, "74F")
