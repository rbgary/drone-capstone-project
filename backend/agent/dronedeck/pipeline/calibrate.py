"""Calibration against a KNOWN heat source — the hand-warmer flights (story
4.6, "define normal thresholds"). `dronedeck calibrate`.

The classifier's minimums (blocks.MIN_EVENT_C / MIN_EVENT_HPA) and the
interpreter's severity factors (findings.WARNING_FACTOR / CRITICAL_FACTOR) are
PROVISIONAL: they were measured on normal flights only — how much a flight
wanders when nothing is there (ml/anomaly-eval/MEASUREMENTS.txt). What a real
heat source does to this sensor has never been measured. This measures it:

  1. Fly a mission with a hand warmer (or any steady heat source) placed at one
     inspection point, at the height the drone holds there. Note the point.
  2. `dronedeck calibrate --flight <id> --at P2` (repeat --flight/--at for
     several flights; the more, the firmer the numbers).
  3. For each marked point it reports what the sensor saw there — the largest
     departure from the flight's own cooling curve while holding, in °C and in
     noise units — and whether the current settings find it and how severe they
     call it. Over the rest of the same flights it reports the most a block
     wanders with nothing there.
  4. It RECOMMENDS constants with the measurements behind them, and saves the
     report to <data folder>/calibration/. It never edits the code: changing a
     threshold is a reviewed change, cited in MEASUREMENTS.txt.

Nothing here flies, connects or uploads.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from dronedeck.pipeline.contracts import FlightContext
from dronedeck.pipeline.sources import LocalFlightSource
from dronedeck.pipeline.stages.classify import blocks
from dronedeck.pipeline.stages.classify.pelt import pelt
from dronedeck.pipeline.stages.clean.robust import RobustCleaner
from dronedeck.pipeline.stages.interpret import findings


@dataclass(frozen=True)
class Marked:
    """One flight and the point the heat source sat at."""

    flight_id: str
    point_id: str


@dataclass(frozen=True)
class PointMeasure:
    flight_id: str
    point_id: str
    readings: int
    noise_c: float
    peak_c: float                 # the largest |residual block mean| at the point
    peak_z: float
    detected: bool                # would the current settings raise an event there
    severity: str | None          # what the current settings would call it


@dataclass(frozen=True)
class Report:
    created_at: str
    points: tuple[PointMeasure, ...]
    normal_wander_c: float        # the most a block wanders elsewhere on these flights
    current: dict[str, float]
    recommended: dict[str, float]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _blocks(residual: np.ndarray, sigma: float) -> list[tuple[int, int, float]]:
    if sigma <= 0 or len(residual) < blocks.MIN_BLOCK:
        return []
    out, start = [], 0
    for end in pelt(residual, blocks.PEN * sigma ** 2 * math.log(max(len(residual), 2)),
                    blocks.MIN_BLOCK):
        out.append((start, end, float(residual[start:end].mean())))
        start = end
    return out


def measure(marked: Sequence[Marked], source: LocalFlightSource, workdir: Path) -> Report:
    points: list[PointMeasure] = []
    wander: list[float] = []
    for m in marked:
        flight = source.load(m.flight_id)
        if m.point_id not in {p.id for p in flight.plan}:
            raise ValueError(f"{m.point_id} is not an inspection point of flight {m.flight_id}")
        ctx = FlightContext(flight.flight_id, flight.session_id, flight.temp_unit, None,
                            flight.started_at, workdir, points=flight.plan)
        data = flight.whole
        clean = RobustCleaner().clean(data, ctx)
        series = blocks._temperature(data.readings, blocks._airborne(data.readings), clean,
                                     flight.temp_unit)
        if isinstance(series, str):
            raise ValueError(f"flight {m.flight_id}: {series}")
        fit = blocks.fit_cooling(series.t, series.y)
        residual = series.y - fit.expected
        sigma = blocks.noise(residual)
        at_point = np.array([data.readings[int(i)].point_id == m.point_id
                             for i in series.positions])
        peak = 0.0
        for run in blocks._runs(series.positions, data.readings):
            r, here = residual[run], at_point[run]
            for a, b, level in _blocks(r, sigma):
                if here[a:b].any():
                    peak = max(peak, level, key=abs)
                else:
                    wander.append(abs(level))
        classified = blocks.BlocksClassifier().classify(data, clean, ctx)
        here_events = [e for e in classified.events if m.point_id in e.point_ids]
        severity = None
        if here_events:
            worst = max(here_events, key=lambda e: abs(e.peak))
            severity = findings.severity(worst, classified.features)
        points.append(PointMeasure(
            m.flight_id, m.point_id, int(at_point.sum()), round(sigma, 4), round(peak, 3),
            round(abs(peak) / sigma, 1) if sigma > 0 else 0.0, bool(here_events), severity))

    normal = max(wander) if wander else 0.0
    current = {"MIN_EVENT_C": blocks.MIN_EVENT_C, "WARNING_FACTOR": findings.WARNING_FACTOR,
               "CRITICAL_FACTOR": findings.CRITICAL_FACTOR}
    recommended = dict(current)
    notes: list[str] = []
    if normal > blocks.MIN_EVENT_C:
        recommended["MIN_EVENT_C"] = math.ceil(normal * 10) / 10
        notes.append(f"Blocks wandered up to {normal:.2f} °C on these flights with nothing "
                     f"there — above MIN_EVENT_C {blocks.MIN_EVENT_C}: raise it.")
    weakest = min((abs(p.peak_c) for p in points), default=0.0)
    if points and weakest < recommended["MIN_EVENT_C"]:
        notes.append(f"The weakest marked point peaked at {weakest:.2f} °C — below the event "
                     f"minimum: the heat source was too weak or too far to be told apart "
                     f"from a normal flight. Move it closer, or use a warmer one.")
    elif points:
        # The weakest real heat source should read at least "warning".
        factor = math.floor(weakest / recommended["MIN_EVENT_C"] * 10) / 10
        if factor < findings.WARNING_FACTOR:
            recommended["WARNING_FACTOR"] = max(1.1, factor)
            notes.append(f"The weakest marked point peaked at {weakest:.2f} °C = "
                         f"{weakest / recommended['MIN_EVENT_C']:.1f}× the minimum, which the "
                         f"current factor ({findings.WARNING_FACTOR}) calls slight: a "
                         f"WARNING_FACTOR of {recommended['WARNING_FACTOR']} makes it a "
                         f"warning.")
    missed = [p for p in points if not p.detected]
    if missed:
        notes.append(f"{len(missed)} of {len(points)} marked points raised no event with the "
                     f"current settings.")
    if not notes:
        notes.append("The current settings find every marked point and keep normal "
                      "wander below the minimum — no change needed.")
    return Report(datetime.now(UTC).isoformat(), tuple(points), round(normal, 3), current,
                  recommended, tuple(notes))


def save(report: Report, data_root: Path) -> Path:
    folder = data_root / "calibration"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"calibration-{report.created_at[:19].replace(':', '-')}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return path
