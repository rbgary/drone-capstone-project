"""Thermal correction for the onboard BMP388.

The barometer sits on a board that heats itself. Raw temperature therefore reads
high by an amount that depends on how hard the motors are working and how long
the drone has been powered. This engine estimates true ambient temperature by
tracking two quantities on different timescales:

  * ``idle_offset`` — how much the electronics add at rest. Drifts slowly.
  * ``ambient`` — the room. Changes when the drone moves somewhere new.

A state machine separates the two. In STABLE the offset is nudged and ambient
tracked gently; when the readings stop matching expectations for several samples
running, it enters TRANSITION and re-bases ambient more aggressively, but only
after the temperature trend has settled.

The tunable constants below are ported verbatim from the original capstone
implementation (``lawnmower_flight.py`` ``ThermalEngine``). They were derived
from flights against a reference thermometer — validated at 0.12 °F mean
absolute error against a 72 °F reference — and cannot be re-derived from first
principles. Change them only with new flight data to justify it.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import StrEnum

from dronedeck.telemetry.heat_bias import HeatBiasModel

# Dry-air gas constant, J/(kg·K). Used for the air-density calculation.
R_DRY_AIR = 287.05
# ISA standard sea-level pressure, hPa.
P_STANDARD = 1013.25


class Mode(StrEnum):
    STABLE = "STABLE"
    TRANSITION = "TRANSITION"


class ThermalState(StrEnum):
    IDLE = "IDLE"
    FLIGHT_COOLING = "FLIGHT (COOLING)"
    FLIGHT_POWER = "FLIGHT (POWER)"


@dataclass(frozen=True)
class CorrectionTuning:
    """Tuned constants. Ported verbatim — see module docstring."""

    # EMA smoothing factor on raw temperature.
    alpha: float = 0.12

    # Rate-of-change thresholds, °C/s.
    heat_thresh: float = 0.07
    cool_thresh: float = 0.03
    roc_stable_thresh: float = 0.02

    # How far the reading may sit from expectation before it means something, °C.
    drift_band_c: float = 0.45
    env_error_c: float = 1.00
    ambient_cool_margin_c: float = 0.30

    # Gains. Ambient moves slowly in STABLE, quickly once TRANSITION settles.
    offset_gain: float = 0.05
    ambient_gain_stable: float = 0.03
    ambient_gain_transition: float = 0.15

    # Hysteresis: how many consecutive samples before believing a change.
    env_sustain_samples: int = 4
    stable_sustain_samples: int = 4
    transition_settle_required: int = 6

    # Largest single correction to ambient, °C. Stops one bad sample lurching it.
    ambient_step_cap_c: float = 0.40

    # Thrust-dependent scaling of the idle offset: airflow cools the board.
    thrust_cooling_ceiling: int = 35000
    scale_idle: float = 1.00
    scale_cooling: float = 0.62
    scale_power: float = 0.78

    # How fast the board cools once the motors start, s. NOT from the original
    # capstone: the scale above is where the offset ends up, but the board
    # takes tens of seconds to get there, so a step to it at takeoff read
    # 2-4.5 °F warm for the first 20 s (lab flights 2026-11-07, 7148a651 and
    # fca56de0, against a 71.1 °F room). The scale now slides from scale_idle
    # to its band's value with this time constant. Replaying both flights,
    # 20 s fit best: mean in-flight error 2.4 °F -> 0.2 °F. 0 = the old step.
    flight_cooling_tau_s: float = 20.0


@dataclass
class CorrectionResult:
    """One corrected sample. All temperatures °C, pressure hPa."""

    raw_temp_c: float
    corrected_temp_c: float
    expected_raw_c: float
    deviation_c: float
    thermal_offset_c: float
    ambient_est_c: float
    roc_c_per_s: float
    air_density_kg_m3: float
    station_pressure_hpa: float
    sea_level_pressure_hpa: float
    absolute_altitude_m: float
    mode: Mode
    state: ThermalState
    event: str
    #: CPU + thrust heat removed on top of the offset (heat_bias.py), °C.
    #: 0.0 unless the engine was given a fitted HeatBiasModel.
    heat_bias_c: float = 0.0


class ThermalEngine:
    """Two-timescale thermal correction.

    Feed it every sample via :meth:`process`. It is stateful and assumes samples
    arrive in order; it uses wall-clock deltas for rate-of-change, so do not
    replay history through a live instance.
    """

    def __init__(
        self,
        initial_raw_c: float,
        initial_ambient_c: float,
        tuning: CorrectionTuning | None = None,
        heat_bias: HeatBiasModel | None = None,
    ) -> None:
        self.tuning = tuning or CorrectionTuning()
        #: Optional CPU-warm-up and thrust-heat correction (sprint 3, task 7).
        #: None keeps the validated behaviour exactly.
        self.heat_bias = heat_bias
        self._started = time.monotonic()

        self._ema_c = initial_raw_c
        self._prev_ema_c = initial_raw_c

        self.ambient_est_c = initial_ambient_c
        # What the electronics contribute at rest, measured at startup.
        self.idle_offset_c = initial_raw_c - initial_ambient_c

        self.mode = Mode.STABLE
        self.state = ThermalState.IDLE
        self.event = "Initialized"

        self._last_update = time.monotonic()
        # When the motors last started, for the takeoff cooling ramp; None on the ground.
        self._flight_started: float | None = None
        self._env_suspect = 0
        self._stable_suspect = 0
        self._transition_settled = 0

    # ── state ────────────────────────────────────────────────────────────

    def _scaling_for(self, thrust: int, now: float) -> float:
        """Airflow over the board scales the idle offset down in flight —
        gradually, because the board takes time to cool (flight_cooling_tau_s)."""
        t = self.tuning
        if thrust == 0:
            self.state = ThermalState.IDLE
            self._flight_started = None
            return t.scale_idle
        if self._flight_started is None:
            self._flight_started = now
        if thrust < t.thrust_cooling_ceiling:
            self.state = ThermalState.FLIGHT_COOLING
            target = t.scale_cooling
        else:
            self.state = ThermalState.FLIGHT_POWER
            target = t.scale_power
        if t.flight_cooling_tau_s <= 0:
            return target
        settled = 1.0 - math.exp(-(now - self._flight_started) / t.flight_cooling_tau_s)
        return t.scale_idle + (target - t.scale_idle) * settled

    def _enter_transition(self, reason: str) -> None:
        self.mode = Mode.TRANSITION
        self._env_suspect = 0
        self._stable_suspect = 0
        self._transition_settled = 0
        self.event = f"ENV CHANGE -> TRANSITION ({reason})"

    def _exit_transition(self) -> None:
        self.mode = Mode.STABLE
        self._env_suspect = 0
        self._stable_suspect = 0
        self._transition_settled = 0
        self.event = "Re-baselined -> STABLE"

    # ── main entry point ─────────────────────────────────────────────────

    def process(self, raw_temp_c: float, thrust: int, pressure_hpa: float) -> CorrectionResult:
        t = self.tuning

        now = time.monotonic()
        dt = max(now - self._last_update, 1e-3)
        self._last_update = now

        self._ema_c = t.alpha * raw_temp_c + (1.0 - t.alpha) * self._ema_c
        roc = (self._ema_c - self._prev_ema_c) / dt
        self._prev_ema_c = self._ema_c

        scaling = self._scaling_for(thrust, now)
        expected_raw_c = self.ambient_est_c + self.idle_offset_c * scaling
        deviation_c = self._ema_c - expected_raw_c
        corrected_c = self._ema_c - self.idle_offset_c * scaling

        # Only re-estimate while the motors are off. In flight, airflow makes
        # the thermal model unreliable and the estimates would chase noise.
        bias_c = 0.0
        if self.heat_bias is not None:
            bias_c = self.heat_bias.update(now - self._started, thrust)
            expected_raw_c += bias_c
            deviation_c -= bias_c
            corrected_c -= bias_c

        if thrust == 0:
            expected_raw_c, deviation_c = self._update_estimates(
                roc, deviation_c, corrected_c, expected_raw_c
            )
            corrected_c = self._ema_c - self.idle_offset_c * scaling - bias_c

        return CorrectionResult(
            raw_temp_c=raw_temp_c,
            corrected_temp_c=corrected_c,
            expected_raw_c=expected_raw_c,
            deviation_c=deviation_c,
            thermal_offset_c=self.idle_offset_c,
            ambient_est_c=self.ambient_est_c,
            roc_c_per_s=roc,
            air_density_kg_m3=air_density(pressure_hpa, corrected_c),
            station_pressure_hpa=pressure_hpa,
            sea_level_pressure_hpa=sea_level_pressure(pressure_hpa, corrected_c, 0.0),
            absolute_altitude_m=pressure_altitude(pressure_hpa),
            mode=self.mode,
            state=self.state,
            event=self.event,
            heat_bias_c=bias_c,
        )

    def _update_estimates(
        self, roc: float, deviation_c: float, corrected_c: float, expected_raw_c: float
    ) -> tuple[float, float]:
        t = self.tuning
        near_expected = abs(deviation_c) <= t.drift_band_c
        settled = abs(roc) <= t.roc_stable_thresh

        # Two ways to suspect the room changed: the reading dropped below
        # ambient outright, or it has drifted far from expectation.
        dropped_below_ambient = self._ema_c <= (self.ambient_est_c - t.ambient_cool_margin_c)
        drifted_far = abs(deviation_c) >= t.env_error_c

        if self.mode is Mode.STABLE:
            if dropped_below_ambient or drifted_far:
                self._env_suspect += 1
                self.event = "ENV CHANGE SUSPECTED"
                if self._env_suspect >= t.env_sustain_samples:
                    self._enter_transition("force" if dropped_below_ambient else "deviation")
            else:
                self._env_suspect = 0

                slow_heating = 0.0 < roc < t.heat_thresh
                slow_cooling = roc < 0.0 and abs(roc) < t.cool_thresh
                if near_expected and (slow_heating or slow_cooling):
                    self.idle_offset_c += deviation_c * t.offset_gain
                    self.event = "Offset drift update (STABLE)"
                else:
                    self.event = "Stable tracking"

                if settled and near_expected:
                    self.ambient_est_c += (corrected_c - self.ambient_est_c) * t.ambient_gain_stable

        else:  # TRANSITION
            self._transition_settled = self._transition_settled + 1 if settled else 0

            if self._transition_settled < t.transition_settle_required:
                # Wait for the trend to settle before re-basing. Re-basing too
                # early was the original failure mode: moving from cold to warm
                # produced an unrealistic corrected temperature.
                self._stable_suspect = 0
                self.event = (
                    f"TRANSITION waiting for settle "
                    f"({self._transition_settled}/{t.transition_settle_required})"
                )
            else:
                step = (corrected_c - self.ambient_est_c) * t.ambient_gain_transition
                step = max(-t.ambient_step_cap_c, min(t.ambient_step_cap_c, step))
                self.ambient_est_c += step

                expected_raw_c = self.ambient_est_c + self.idle_offset_c
                deviation_c = self._ema_c - expected_raw_c

                if abs(deviation_c) <= t.drift_band_c and settled:
                    self._stable_suspect += 1
                    self.event = (
                        f"TRANSITION settling "
                        f"({self._stable_suspect}/{t.stable_sustain_samples})"
                    )
                    if self._stable_suspect >= t.stable_sustain_samples:
                        self._exit_transition()
                else:
                    self._stable_suspect = 0
                    self.event = "TRANSITION (re-basing ambient)"

        return expected_raw_c, deviation_c


# ── derived environmental quantities ────────────────────────────────────


def pressure_altitude(pressure_hpa: float) -> float:
    """Altitude above ISA sea level, metres. Supporting metric only —
    Lighthouse provides the authoritative position."""
    return 44330.0 * (1.0 - math.pow(pressure_hpa / P_STANDARD, 0.1903))


def air_density(pressure_hpa: float, temp_c: float) -> float:
    """Dry-air density, kg/m³, from corrected temperature."""
    return (pressure_hpa * 100.0) / (R_DRY_AIR * (temp_c + 273.15))


def sea_level_pressure(pressure_hpa: float, temp_c: float, station_alt_m: float) -> float:
    """QFF — station pressure reduced to sea level."""
    return pressure_hpa * math.pow(
        1.0 - (0.0065 * station_alt_m) / (temp_c + 273.15 + 0.0065 * station_alt_m),
        -5.257,
    )


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0
