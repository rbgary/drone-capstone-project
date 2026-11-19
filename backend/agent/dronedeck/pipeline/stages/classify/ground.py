"""The ground classifier — stage 3 for a SESSION: the stretches of a session
when the drone sat on the ground (before, between and after its flights), at
one reading a second.

WHY A SECOND CLASSIFIER. blocks@1 judges a flight against the board COOLING in
the propellers' air, at 10 Hz. On the ground the motors are off and the board
does the opposite — the electronics warm it, towards a level it settles at —
and the session records once a second. The model is the same family (a
first-order curve, T∞ + A·e^(−t/τ), which fits warming as well as cooling, and
a robust line for pressure), fitted over each ground stretch on its own, so a
flight's cooling never counts as a ground event.

WHAT IT LEAVES OUT. Every reading in or within GROUND_SETTLE_S of a flight
(the flights have results of their own, at ten times the rate), and stretches
shorter than MIN_GROUND_READINGS. On the ground the height does not change, so
pressure is judged as measured ("linear, on the ground").

The block-finding is blocks@1's own — PELT on the residual, Z_EVENT × σ AND a
physical minimum — with numbers measured at 1 Hz on the lab's sessions
(ml/session-eval/MEASUREMENTS.txt): the shortest block (MIN_GROUND_BLOCK), the
gap that ends a run, and the minimum departures (the most a normal session's
ground blocks wander).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

from dronedeck.pipeline.contracts import (
    ClassifyResult,
    CleanResult,
    Event,
    FlightContext,
    Label,
    PointData,
    Reading,
    Segment,
    Track,
)
from dronedeck.pipeline.stages.classify.blocks import (
    Series,
    Tuning,
    _analyse,
    _temperature,
    _usable,
)

#: The phase a session reading is in (Reading.text["phase"], pipeline/sources.py).
GROUND = "ground"
#: Readings this close to a flight are left out: the motors spinning up, and the
#: board's first seconds after landing (ml/session-eval/MEASUREMENTS.txt).
GROUND_SETTLE_S = 10.0
#: A stretch shorter than this — 30 s at 1 Hz — is not fitted. 30 keeps 67 of
#: the corpus's 202 ground stretches (60 kept 43) with no false event
#: (ml/session-eval/MEASUREMENTS.txt, "Grid").
MIN_GROUND_READINGS = 30
#: The shortest block, 10 s (MEASUREMENTS.txt, "Grid").
MIN_GROUND_BLOCK = 10
#: 2.5 periods at 1 Hz: the cleaner's gap limit (MEASUREMENTS.txt, "Timing").
GROUND_RUN_GAP_S = 2.5
#: A ground block's mean wanders up to 0.93 °C and 0.16 hPa on a normal session
#: (1 098 and 381 blocks): the minimums sit above that (MEASUREMENTS.txt, "Wander").
MIN_GROUND_EVENT_C = 1.0
MIN_GROUND_EVENT_HPA = 0.2

GROUND_TUNING = Tuning(min_block=MIN_GROUND_BLOCK, run_gap_s=GROUND_RUN_GAP_S,
                       min_event_c=MIN_GROUND_EVENT_C, min_event_hpa=MIN_GROUND_EVENT_HPA)


def ground_stretches(readings: Sequence[Reading],
                     settle_s: float = GROUND_SETTLE_S) -> list[NDArray[np.intp]]:
    """Positions of each run of ground readings, trimmed by `settle_s` on any
    side that touches a flight."""
    stretches: list[NDArray[np.intp]] = []
    run: list[int] = []

    def close(next_is_flight: bool) -> None:
        if not run:
            return
        first, last = readings[run[0]], readings[run[-1]]
        after_flight = run[0] > 0
        start = first.t_s + (settle_s if after_flight else 0.0)
        end = last.t_s - (settle_s if next_is_flight else 0.0)
        kept = [i for i in run if start <= readings[i].t_s <= end]
        if kept:
            stretches.append(np.array(kept, dtype=np.intp))
        run.clear()

    for i, r in enumerate(readings):
        if r.text.get("phase", GROUND) == GROUND:
            run.append(i)
        else:
            close(next_is_flight=True)
    close(next_is_flight=False)
    return stretches


def _pressure(readings: Sequence[Reading], positions: NDArray[np.intp],
              clean: CleanResult, minimum: int) -> Series | str:
    keep = _usable(readings, positions, clean, "station_pressure_hpa")
    if len(keep) < minimum:
        return f"too few usable pressure readings on the ground ({len(keep)}; {minimum} needed)"
    y = np.array([float(readings[int(i)].values["station_pressure_hpa"] or 0.0) for i in keep],
                 dtype=np.float64)
    t = np.array([readings[int(i)].t_s for i in keep], dtype=np.float64)
    return Series("pressure", "station_pressure_hpa", "hPa", keep, t, y, "on the ground")


def _join(parts: list[Track]) -> Track:
    """One track per signal for the whole session — its ground stretches, each
    against its own fit, in order."""
    first = parts[0]
    noises = [p.noise for p in parts if p.noise is not None and math.isfinite(p.noise)]
    model = first.model if "on the ground" in first.model else f"{first.model}, on the ground"
    return Track(first.signal, first.column, first.unit, model,
                 max(noises) if noises else None,
                 tuple(i for p in parts for i in p.indexes),
                 tuple(v for p in parts for v in p.observed),
                 tuple(v for p in parts for v in p.expected))


class GroundClassifier:
    name = "ground"
    version = "1"

    def classify(self, data: PointData, clean: CleanResult,
                 ctx: FlightContext) -> ClassifyResult:
        model = f"{self.name}@{self.version}"
        stretches = [s for s in ground_stretches(data.readings)
                     if len(s) >= MIN_GROUND_READINGS]
        parts: dict[str, list[Track]] = {"temperature": [], "pressure": []}
        segments: list[Segment] = []
        events: list[Event] = []
        features: dict[str, float] = {
            "ground_stretches": float(len(stretches)),
            "readings_on_the_ground": float(sum(len(s) for s in stretches)),
            "temp_event_minimum_c": MIN_GROUND_EVENT_C,
            "pressure_event_minimum_hpa": MIN_GROUND_EVENT_HPA,
        }
        why_not: list[str] = []
        if not stretches:
            why_not.append(f"no stretch on the ground of at least {MIN_GROUND_READINGS} "
                           f"readings")
        for stretch in stretches:
            for series in (_temperature(data.readings, stretch, clean, ctx.temp_unit),
                           _pressure(data.readings, stretch, clean, MIN_GROUND_READINGS)):
                if isinstance(series, str):
                    why_not.append(series.replace("in the air", "on the ground"))
                    continue
                track, found, fevents, feats = _analyse(series, data.readings, clean, ctx,
                                                        GROUND_TUNING)
                parts[series.signal].append(track)
                segments += found
                events += fevents
                for key, value in feats.items():
                    if key.endswith(("_noise_c", "_noise_hpa", "_spread_c", "_spread_hpa")):
                        features[key] = max(features.get(key, 0.0), value)
        tracks = tuple(_join(p) for p in parts.values() if p)
        if events:
            sensors = Label("faulty", None, model)
        elif tracks:
            sensors = Label("normal", None, model)
        else:
            sensors = Label("unknown", None, model,
                            "; ".join(dict.fromkeys(why_not)) or "nothing to analyse")
        return ClassifyResult(sensors=sensors, features=features,
                              tracks=tracks, segments=tuple(segments),
                              events=tuple(sorted(events, key=lambda e: e.start_index)))
