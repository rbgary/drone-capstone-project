"""CPU and thrust heat biases on the barometer's temperature (sprint 3, task 7).

The BMP388 sits on the same board as the STM32 and beside the motor drivers,
so its temperature reads the room PLUS heat the drone makes itself. Matthew's
ThermalEngine (correction.py, ported from flight_sensor_test_with_recovery_and_
geofencing.py) handles that with ONE offset measured at startup, scaled by a
three-step thrust table (1.00 / 0.62 / 0.78). Two biases that table cannot
follow:

  CPU WARM-UP   After power-on the board keeps warming for minutes. An offset
                measured at startup is too small a few minutes later, so the
                corrected temperature creeps up with the room unchanged.
                Modelled as a first-order rise:
                    cpu(t) = cpu_amp_c * (1 - exp(-t / cpu_tau_s))

  THRUST HEAT   The motors and their drivers heat the board in proportion to
                how hard they work, and that heat arrives and leaves slowly. A
                step table switches instantly. Modelled as thrust fraction
                through a first-order lag:
                    d(heat)/dt = (thrust_gain_c * thrust/65535 - heat) / thrust_tau_s

The correction subtracts how much each bias has GROWN since the engine started
(the startup offset already contains whatever was there then).

STARTER CODE. Every coefficient defaults to 0, so the model changes nothing
until it is fitted from data — the existing constants were validated against a
reference thermometer and are not touched. `fit_cpu_warmup` fits the CPU curve
from a motors-off recording (task 8's sensor-only test makes one);
fitting the thrust terms needs a recording with the motors running.
"""

from __future__ import annotations

import csv
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

THRUST_MAX = 65535


@dataclass(frozen=True)
class HeatBiasTuning:
    """Coefficients. All zero = no correction (the default until fitted)."""

    cpu_amp_c: float = 0.0       # how far the board warms above its startup offset, °C
    cpu_tau_s: float = 300.0     # how fast it gets there, s (63 % of the way)
    thrust_gain_c: float = 0.0   # heat at full thrust once settled, °C
    thrust_tau_s: float = 20.0   # how fast thrust heat arrives and leaves, s

    @property
    def enabled(self) -> bool:
        return self.cpu_amp_c != 0.0 or self.thrust_gain_c != 0.0


#: The CPU warm-up fitted from the first bench run on the lab drone
#: (thermal-bench-20261107-155544.csv: 5 min, motors off, room 71.3 °F on a
#: thermometer; rms 0.09 °C). Valid only when the engine starts as the drone
#: powers on cold — the bench test's case, NOT a flight's (a flight's engine
#: starts at takeoff, minutes after power-on). Used by `dronedeck thermal-test`.
BENCH_CPU_WARMUP = HeatBiasTuning(cpu_amp_c=6.14, cpu_tau_s=130.0)


class HeatBiasModel:
    """Feed it each sample's time since start and thrust; it returns the bias
    to subtract from the corrected temperature, °C."""

    def __init__(self, tuning: HeatBiasTuning | None = None) -> None:
        self.tuning = tuning or HeatBiasTuning()
        self._thrust_heat_c = 0.0
        self._last_t: float | None = None

    def cpu_bias_c(self, elapsed_s: float) -> float:
        t = self.tuning
        if t.cpu_amp_c == 0.0 or t.cpu_tau_s <= 0:
            return 0.0
        return t.cpu_amp_c * (1.0 - math.exp(-max(elapsed_s, 0.0) / t.cpu_tau_s))

    def update(self, elapsed_s: float, thrust: int) -> float:
        """Advance to `elapsed_s` (seconds since the engine started) and return
        the total extra bias, °C, since then."""
        t = self.tuning
        dt = 0.0 if self._last_t is None else max(elapsed_s - self._last_t, 0.0)
        self._last_t = elapsed_s
        if t.thrust_gain_c != 0.0 and t.thrust_tau_s > 0 and dt > 0:
            target = t.thrust_gain_c * max(0, min(thrust, THRUST_MAX)) / THRUST_MAX
            # Exact first-order step, stable for any dt.
            step = 1 - math.exp(-dt / t.thrust_tau_s)
            self._thrust_heat_c += (target - self._thrust_heat_c) * step
        return self.cpu_bias_c(elapsed_s) + self._thrust_heat_c

    @property
    def thrust_heat_c(self) -> float:
        return self._thrust_heat_c


# ── fitting ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CpuFit:
    cpu_amp_c: float
    cpu_tau_s: float
    rms_error_c: float
    samples: int


def fit_cpu_warmup(
    elapsed_s: Sequence[float],
    raw_c: Sequence[float],
    taus_s: Sequence[float] = tuple(range(20, 1801, 10)),
) -> CpuFit:
    """Fit raw(t) = raw(0) + A * (1 - exp(-t/tau)) to a MOTORS-OFF recording in
    a room whose temperature did not change. For each candidate tau the best A
    is closed-form least squares; the tau with the smallest error wins."""
    if len(elapsed_s) != len(raw_c) or len(raw_c) < 10:
        raise ValueError("need at least 10 paired samples")
    t0 = elapsed_s[0]
    ts = [t - t0 for t in elapsed_s]
    # Average the first few samples as the start: one noisy reading should not
    # set the baseline.
    head = raw_c[: min(5, len(raw_c))]
    base = sum(head) / len(head)
    ys = [r - base for r in raw_c]

    best: CpuFit | None = None
    for tau in taus_s:
        xs = [1.0 - math.exp(-t / tau) for t in ts]
        sxx = sum(x * x for x in xs)
        if sxx == 0:
            continue
        amp = sum(x * y for x, y in zip(xs, ys, strict=True)) / sxx
        rms = math.sqrt(sum((y - amp * x) ** 2 for x, y in zip(xs, ys, strict=True)) / len(xs))
        if best is None or rms < best.rms_error_c:
            best = CpuFit(round(amp, 4), float(tau), round(rms, 4), len(xs))
    if best is None:
        raise ValueError("no candidate time constant fitted")
    return best


def fit_cpu_warmup_csv(path: Path) -> CpuFit:
    """Fit from a CSV with `elapsed_s` and `raw_temp_c` columns — what task 8's
    `dronedeck thermal-test` writes. Rows with thrust above 0 are skipped."""
    ts: list[float] = []
    raw: list[float] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if float(row.get("thrust") or 0) > 0:
                continue
            ts.append(float(row["elapsed_s"]))
            raw.append(float(row["raw_temp_c"]))
    return fit_cpu_warmup(ts, raw)


def main(argv: Sequence[str] | None = None) -> int:
    """python -m dronedeck.telemetry.heat_bias fit recording.csv"""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "fit":
        print("usage: python -m dronedeck.telemetry.heat_bias fit <recording.csv>")
        return 2
    fit = fit_cpu_warmup_csv(Path(args[1]))
    print(f"cpu_amp_c={fit.cpu_amp_c}  cpu_tau_s={fit.cpu_tau_s}  "
          f"rms_error_c={fit.rms_error_c}  samples={fit.samples}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
