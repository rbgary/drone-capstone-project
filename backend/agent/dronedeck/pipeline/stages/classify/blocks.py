"""The classifier — stage 3, story 4.6: find the BLOCKS of a flight where the
temperature or the pressure departs from what was expected, and say so in
numbers. The telemetry is the evidence; frames are labelled, never judged
(no image model exists yet — story 4.4).

WHAT "EXPECTED" MEANS, AND WHY (ml/anomaly-eval/MEASUREMENTS.txt)

  Temperature  The barometer sits on the drone's own board, which COOLS in the
               propellers' air from the moment it takes off — 34.7 → 29.6 °C
               in 48 s on a normal mission. So "unusual" is measured against
               that curve: raw_temp (the sensor itself, in °C) is fitted with
               a first-order cooling curve, T∞ + A·e^(−t/τ), robustly (Huber
               weights), over the airborne readings. corrected_temp is not
               used: it steps whenever the correction engine's thermal_state
               switches, which is the engine, not the room.
  Pressure     A barometer reads ~0.12 hPa lower per metre climbed, so
               station pressure is corrected for height with physics —
               + ρ·g·z / 100, ρ from the row's own air density — never with a
               per-flight fit, which would absorb a real event that happened
               at one height. Rows whose height the drone did not measure are
               left out; with too few, the pressure is used uncorrected and
               the track says so ("linear, height not measured"). Expected: a
               robust straight line (the room's slow drift).

THE BLOCKS: the residual (observed − expected) is split into blocks of
constant level by PELT (pelt.py), penalty PEN × σ² × ln n, every block at
least MIN_BLOCK readings. σ is the residual's noise from its first
differences (1.4826 × MAD / √2), which a block does not inflate. A block
departs when its mean is at least Z_EVENT × σ AND a physical minimum
(MIN_EVENT_C, MIN_EVENT_HPA — the most a normal flight's blocks wander);
adjacent departing blocks that go the same way are ONE event (a ramp up and
down is a staircase of blocks), carrying its mean departure and its peak.
Whether an event matters — a severity — is interpret's call, not this stage's.

Flagged values never reach a model: only clean.usable values are fitted.
Every constant cites ml/anomaly-eval/MEASUREMENTS.txt.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from dronedeck.pipeline.contracts import (
    ClassifyResult,
    CleanResult,
    EnhanceResult,
    Event,
    FlightContext,
    ImageVerdict,
    Label,
    PointData,
    Reading,
    Segment,
    Signal,
    Track,
)
from dronedeck.pipeline.stages.classify.pelt import pelt

FloatArray = NDArray[np.float64]

#: Readings after takeoff left out: the correction engine and the board's
#: first gust of air (MEASUREMENTS.txt, "Settle").
SETTLE_S = 3.0
#: Fewer airborne readings than this (3 s) and nothing is fitted.
MIN_READINGS = 30
#: The penalty per change, in units of σ² × ln n (MEASUREMENTS.txt, "Blocks").
PEN = 8.0
#: The shortest block: 2 s at 10 Hz (MEASUREMENTS.txt, "Blocks").
MIN_BLOCK = 20
#: A block departs when its mean is this many noise σ from expected
#: (MEASUREMENTS.txt, "Events")...
Z_EVENT = 4.0
#: ...AND at least this far, physically. A block's mean wanders this much on a
#: normal flight (the board's airflow, the engine's settling): the largest
#: wander measured on the lab's flights sets these (MEASUREMENTS.txt, "Events").
MIN_EVENT_C = 0.8
MIN_EVENT_HPA = 0.2
#: The cooling curve's time constant is searched on this grid, seconds.
TAU_GRID = np.geomspace(5.0, 1200.0, 48)
#: Huber's tuning constant, in noise units (95 % efficiency on normal noise).
HUBER_K = 1.345
#: g, m/s² (standard gravity).
G = 9.80665
#: Air density when the row has none, kg/m³ (ISA sea level).
RHO_DEFAULT = 1.225
#: Below this share of airborne rows with a measured height, pressure is used
#: uncorrected.
MIN_HEIGHT_SHARE = 0.5
MAD_TO_SIGMA = 1.4826
#: More than this between two readings ends a run: a block never spans lost
#: time (2.5 periods at 10 Hz, the cleaner's gap limit).
RUN_GAP_S = 0.25


@dataclass(frozen=True)
class Tuning:
    """The numbers block-finding runs on. A flight's are this module's
    constants; a session's ground stretches have their own, measured at 1 Hz
    (stages/classify/ground.py)."""

    min_block: int = MIN_BLOCK
    run_gap_s: float = RUN_GAP_S
    min_event_c: float = MIN_EVENT_C
    min_event_hpa: float = MIN_EVENT_HPA


FLIGHT_TUNING = Tuning()


@dataclass(frozen=True)
class Fit:
    model: str
    expected: FloatArray
    params: dict[str, float]


@dataclass(frozen=True)
class Series:
    """One signal's usable airborne values, by reading position."""

    signal: Signal
    column: str
    unit: str                       # the unit the TRACK reports in
    positions: NDArray[np.intp]     # into data.readings
    t: FloatArray
    y: FloatArray                   # in the unit the model works in (°C, hPa)
    note: str | None = None


# ── models ───────────────────────────────────────────────────────────────


def _mad(x: FloatArray) -> float:
    return float(np.median(np.abs(x - np.median(x)))) if len(x) else 0.0


def _huber_weights(residual: FloatArray, scale: float) -> FloatArray:
    if scale <= 0:
        return np.ones_like(residual)
    a = np.abs(residual) / (HUBER_K * scale)
    return np.asarray(np.where(a <= 1.0, 1.0, 1.0 / np.maximum(a, 1e-12)), dtype=np.float64)


def _weighted_lsq(basis: FloatArray, y: FloatArray, w: FloatArray) -> FloatArray:
    sw = np.sqrt(w)
    coef, *_ = np.linalg.lstsq(basis * sw[:, None], y * sw, rcond=None)
    return np.asarray(coef, dtype=np.float64)


def _floor(y: FloatArray) -> float:
    """The smallest scale a robust fit may use: the data's own reading-to-
    reading noise. Without it, a near-perfect fit drives the scale to zero and
    calls every point an outlier."""
    return max(noise(y), 1e-6)


def _irls(basis: FloatArray, y: FloatArray, floor: float,
          rounds: int = 6) -> tuple[FloatArray, float]:
    """Huber-robust least squares. Returns the coefficients and the Huber loss
    in units of `floor` — ONE scale for every candidate, so losses compare."""
    w = np.ones_like(y)
    coef = _weighted_lsq(basis, y, w)
    for _ in range(rounds):
        # The scale is the data's own reading-to-reading noise, FIXED: a big
        # event must not buy itself a lenient scale and bend the curve to it.
        w = _huber_weights(y - basis @ coef, floor)
        coef = _weighted_lsq(basis, y, w)
    a = np.abs(y - basis @ coef) / (HUBER_K * floor)
    loss = float(np.sum(np.where(a <= 1, 0.5 * a * a, a - 0.5)))
    return coef, loss


def fit_line(t: FloatArray, y: FloatArray) -> Fit:
    basis = np.column_stack((np.ones_like(t), t - t[0]))
    coef, _ = _irls(basis, y, _floor(y))
    return Fit("linear", basis @ coef, {"level": float(coef[0]),
                                         "slope_per_s": float(coef[1])})


def fit_cooling(t: FloatArray, y: FloatArray) -> Fit:
    """T∞ + A·e^(−(t − t0)/τ): τ on a grid, (T∞, A) by robust least squares,
    the τ with the least Huber loss on one shared scale."""
    t0 = float(t[0])
    floor = _floor(y)
    best: tuple[float, float] | None = None
    for candidate in TAU_GRID:
        basis = np.column_stack((np.ones_like(t), np.exp(-(t - t0) / candidate)))
        _, loss = _irls(basis, y, floor, rounds=3)
        if best is None or loss < best[1]:
            best = (float(candidate), loss)
    assert best is not None
    tau = best[0]
    basis = np.column_stack((np.ones_like(t), np.exp(-(t - t0) / tau)))
    coef, _ = _irls(basis, y, floor)
    return Fit("cooling-curve", basis @ coef,
               {"settles_at": float(coef[0]), "amplitude": float(coef[1]), "tau_s": tau})


def noise(residual: FloatArray) -> float:
    """σ from first differences: blind to a block, which a plain MAD is not."""
    if len(residual) < 3:
        return 0.0
    return MAD_TO_SIGMA * _mad(np.diff(residual)) / math.sqrt(2.0)


# ── the classifier ───────────────────────────────────────────────────────


class BlocksClassifier:
    name = "blocks"
    version = "1"

    def classify(self, data: PointData, clean: CleanResult, enhanced: EnhanceResult,
                 ctx: FlightContext) -> ClassifyResult:
        model = f"{self.name}@{self.version}"
        images = _images(enhanced, model)
        airborne = _airborne(data.readings)
        tracks: list[Track] = []
        segments: list[Segment] = []
        events: list[Event] = []
        features: dict[str, float] = {"readings_airborne": float(len(airborne))}
        why_not: list[str] = []

        for series in (_temperature(data.readings, airborne, clean, ctx.temp_unit),
                       _pressure(data.readings, airborne, clean)):
            if isinstance(series, str):
                why_not.append(series)
                continue
            track, found, fevents, feats = _analyse(series, data.readings, clean, ctx)
            tracks.append(track)
            segments += found
            events += fevents
            features.update(feats)

        if events:
            sensors = Label("faulty", None, model)
        elif tracks:
            sensors = Label("normal", None, model)
        else:
            sensors = Label("unknown", None, model, "; ".join(why_not) or "nothing to analyse")
        from dronedeck.pipeline.stages.classify.scene import measure

        return ClassifyResult(images=images, sensors=sensors, features=features,
                              tracks=tuple(tracks), segments=tuple(segments),
                              events=tuple(sorted(events, key=lambda e: e.start_index)),
                              views=measure(enhanced.frames))


def _images(enhanced: EnhanceResult, model: str) -> tuple[ImageVerdict, ...]:
    out = []
    for e in enhanced.frames:
        if e.quality is not None and not e.quality.usable:
            reason = f"frame unusable — {e.quality.reason}"
        elif e.path is None and e.note and "could not be read" in e.note:
            reason = "frame unreadable"
        else:
            reason = "no image model yet"
        out.append(ImageVerdict(e.frame.seq, "original", Label("unknown", None, model, reason)))
    return tuple(out)


def _airborne(readings: Sequence[Reading]) -> NDArray[np.intp]:
    """Positions of readings in flight, after the takeoff settle. A flight
    whose rows carry no thermal_state (an old CSV) is taken whole."""
    states = [r.text.get("thermal_state") for r in readings]
    if not any(states):
        return np.arange(len(readings), dtype=np.intp)
    flying = [i for i, s in enumerate(states) if s and s.startswith("FLIGHT")]
    if not flying:
        return np.array([], dtype=np.intp)
    takeoff = readings[flying[0]].t_s
    return np.array([i for i in flying if readings[i].t_s - takeoff >= SETTLE_S],
                    dtype=np.intp)


def _usable(readings: Sequence[Reading], positions: NDArray[np.intp], clean: CleanResult,
            column: str) -> NDArray[np.intp]:
    keep = []
    for i in positions:
        r = readings[int(i)]
        v = r.values.get(column)
        if v is not None and math.isfinite(v) and clean.usable(r.index, column):
            keep.append(int(i))
    return np.array(keep, dtype=np.intp)


def _temperature(readings: Sequence[Reading], airborne: NDArray[np.intp],
                 clean: CleanResult, unit: str) -> Series | str:
    keep = _usable(readings, airborne, clean, "raw_temp")
    if len(keep) < MIN_READINGS:
        return (f"too few usable temperature readings in the air ({len(keep)}; "
                f"{MIN_READINGS} needed)")
    raw = np.array([readings[i].values["raw_temp"] or 0.0 for i in keep], dtype=np.float64)
    celsius = (raw - 32.0) * 5.0 / 9.0 if unit == "F" else raw
    t = np.array([readings[i].t_s for i in keep], dtype=np.float64)
    return Series("temperature", "raw_temp", unit, keep, t, celsius)


def _pressure(readings: Sequence[Reading], airborne: NDArray[np.intp],
              clean: CleanResult) -> Series | str:
    keep = _usable(readings, airborne, clean, "station_pressure_hpa")
    if len(keep) < MIN_READINGS:
        return (f"too few usable pressure readings in the air ({len(keep)}; "
                f"{MIN_READINGS} needed)")
    heights = _usable(readings, keep, clean, "z_m")
    if len(heights) >= MIN_HEIGHT_SHARE * len(keep) and len(heights) >= MIN_READINGS:
        keep, note = heights, None
    else:
        note = "height not measured"
    values = []
    for i in keep:
        r = readings[int(i)].values
        p = float(r["station_pressure_hpa"] or 0.0)
        if note is None:
            rho = r.get("air_density_kg_m3")
            rho = rho if rho is not None and math.isfinite(rho) and rho > 0 else RHO_DEFAULT
            p += rho * G * float(r["z_m"] or 0.0) / 100.0
        values.append(p)
    t = np.array([readings[int(i)].t_s for i in keep], dtype=np.float64)
    return Series("pressure", "station_pressure_hpa", "hPa", keep, t,
                  np.array(values, dtype=np.float64), note)


def _to_unit(series: Series, celsius: FloatArray | float, *, difference: bool) -> FloatArray:
    """Model units (°C) → the track's unit. A difference takes × 9/5 only."""
    x = np.asarray(celsius, dtype=np.float64)
    if series.signal != "temperature" or series.unit != "F":
        return x
    return np.asarray(x * 9.0 / 5.0 if difference else x * 9.0 / 5.0 + 32.0, dtype=np.float64)


def _runs(positions: NDArray[np.intp], readings: Sequence[Reading],
          gap_s: float = RUN_GAP_S) -> list[slice]:
    """Stretches of consecutive readings with no time lost between them, so a
    block never spans a gap or a left-out (flagged) reading."""
    if len(positions) == 0:
        return []
    cuts = [0]
    for k in range(1, len(positions)):
        a, b = readings[int(positions[k - 1])], readings[int(positions[k])]
        if positions[k] != positions[k - 1] + 1 or b.t_s - a.t_s > gap_s:
            cuts.append(k)
    cuts.append(len(positions))
    return [slice(a, b) for a, b in zip(cuts, cuts[1:], strict=False)]


def _analyse(series: Series, readings: Sequence[Reading], clean: CleanResult,
             ctx: FlightContext, tuning: Tuning = FLIGHT_TUNING,
             ) -> tuple[Track, list[Segment], list[Event], dict[str, float]]:
    fit = fit_cooling(series.t, series.y) if series.signal == "temperature" \
        else fit_line(series.t, series.y)
    residual = series.y - fit.expected
    sigma = noise(residual)
    spread = MAD_TO_SIGMA * _mad(residual)
    model = fit.model + (f", {series.note}" if series.note else "")
    if series.signal == "temperature":          # the model works in °C
        features = {"temp_noise_c": sigma, "temp_spread_c": spread,
                    "temp_event_minimum_c": tuning.min_event_c,
                    "temp_settles_at_c": fit.params["settles_at"],
                    "temp_cooling_amplitude_c": fit.params["amplitude"],
                    "temp_tau_s": fit.params["tau_s"],
                    "temp_readings_used": float(len(series.y))}
    else:
        features = {"pressure_noise_hpa": sigma, "pressure_spread_hpa": spread,
                    "pressure_event_minimum_hpa": tuning.min_event_hpa,
                    "pressure_level_hpa": fit.params["level"],
                    "pressure_drift_hpa_per_s": fit.params["slope_per_s"],
                    "pressure_readings_used": float(len(series.y))}

    track = Track(series.signal, series.column, series.unit, model,
                  float(_to_unit(series, sigma, difference=True)),
                  tuple(readings[int(i)].index for i in series.positions),
                  tuple(round(float(v), 4) for v in _to_unit(series, series.y, difference=False)),
                  tuple(round(float(v), 4) for v in _to_unit(series, fit.expected,
                                                             difference=False)))

    segments: list[Segment] = []
    events: list[Event] = []
    if sigma <= 0:
        return track, segments, events, features
    minimum = tuning.min_event_c if series.signal == "temperature" else tuning.min_event_hpa
    threshold = max(Z_EVENT * sigma, minimum)
    for run in _runs(series.positions, readings, tuning.run_gap_s):
        r = residual[run]
        if len(r) < tuning.min_block:
            continue        # a stretch between gaps or flags too short to hold a block
        positions = series.positions[run]
        penalty = PEN * sigma * sigma * math.log(max(len(r), 2))
        flagged: list[tuple[int, int, float]] = []      # start, end, level
        start = 0
        for end in pelt(r, penalty, tuning.min_block):
            first, last = readings[int(positions[start])], readings[int(positions[end - 1])]
            level = float(r[start:end].mean())
            segments.append(Segment(series.signal, first.index, last.index, first.t_s,
                                    last.t_s, end - start,
                                    round(float(_to_unit(series, level, difference=True)), 4)))
            if abs(level) >= threshold:
                flagged.append((start, end, level))
            start = end
        for a, b, peak in _merge(flagged):
            events.append(_event(series, readings, clean, positions[a:b], series.y[run][a:b],
                                 fit.expected[run][a:b], float(r[a:b].mean()), peak, sigma))
    return track, segments, events, features


def _merge(blocks: list[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    """Adjacent event blocks that depart the same way are one event — a ramp up
    and down comes out of PELT as a staircase. Returns start, end and the peak
    block's level."""
    merged: list[tuple[int, int, float]] = []
    for start, end, level in blocks:
        if merged and merged[-1][1] == start and (merged[-1][2] > 0) == (level > 0):
            a, _, peak = merged[-1]
            merged[-1] = (a, end, level if abs(level) > abs(peak) else peak)
        else:
            merged.append((start, end, level))
    return merged


def _event(series: Series, readings: Sequence[Reading], clean: CleanResult,
           positions: NDArray[np.intp], observed: FloatArray, expected: FloatArray,
           level: float, peak: float, scale: float) -> Event:
    rows = [readings[int(i)] for i in positions]
    t = np.array([r.t_s for r in rows])
    slope = float(np.polyfit(t - t[0], observed, 1)[0]) if len(t) > 1 else 0.0
    points = tuple(dict.fromkeys(r.point_id for r in rows if r.point_id))
    placed = [r for r in rows
              if all(clean.usable(r.index, c) and r.values.get(c) is not None
                     for c in ("x_m", "y_m", "z_m"))]

    def mean(column: str) -> float | None:
        return round(float(np.mean([r.values[column] for r in placed])), 3) if placed else None

    return Event(
        signal=series.signal, unit=series.unit,
        start_index=rows[0].index, end_index=rows[-1].index,
        t_start_s=rows[0].t_s, t_end_s=rows[-1].t_s,
        direction="rise" if level > 0 else "drop",
        observed=round(float(_to_unit(series, float(observed.mean()), difference=False)), 4),
        expected=round(float(_to_unit(series, float(expected.mean()), difference=False)), 4),
        delta=round(float(_to_unit(series, level, difference=True)), 4),
        z=round(abs(level) / scale, 2),
        peak=round(float(_to_unit(series, peak, difference=True)), 4),
        slope_per_s=round(float(_to_unit(series, slope, difference=True)), 5),
        point_ids=points, x_m=mean("x_m"), y_m=mean("y_m"), z_m=mean("z_m"),
    )
