"""Interpret — stage 4: the classifier's events → findings an inspector can act
on, and a verdict for every inspection point.

THE TELEMETRY IS THE EVIDENCE (the owner, 2026-10-09). A finding is made from
an event in the readings, and from nothing else. The drone carries no camera
(contract v3); photos taken by hand are classified on their own and never
change a finding.

SEVERITY is tied to the measurement, never invented (CLAUDE.md, invariant 1).
The classifier only raises an event beyond the most a NORMAL flight's blocks
wander (blocks.MIN_EVENT_C / MIN_EVENT_HPA, measured on the lab's flights):

    info       an event — beyond any normal flight's wander
    warning    its peak at least WARNING_FACTOR × that minimum
    critical   its peak at least CRITICAL_FACTOR × that minimum

PROVISIONAL until the hand-warmer flights (story 4.6) measure a real heat
source (ml/anomaly-eval/MEASUREMENTS.txt). A pressure event whose height was
not measured is at most info: a climb would look the same.

WORDS come from fixed templates filled with the event's numbers — exact,
testable, and nothing an operator reads was made up.

A SESSION'S GROUND STRETCHES (ctx.scope "session", stages/classify/ground.py)
are said as such: "on the ground", against the sensor's own trend there — never
a cooling curve, an inspection point or a height (on the ground z is the
floor's Lighthouse height, not a height above it).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from dronedeck.pipeline.contracts import (
    Alert,
    ClassifyResult,
    CleanResult,
    Event,
    Finding,
    FlightContext,
    InspectionPoint,
    InterpretResult,
    PointData,
    PointResult,
    Severity,
    Track,
)
from dronedeck.pipeline.stages.classify.blocks import MIN_EVENT_C, MIN_EVENT_HPA

#: The classifier says how far a normal flight wanders (its features
#: "temp_event_minimum_c", "pressure_event_minimum_hpa"); blocks@1's numbers
#: are the fallback for a classifier that does not.

#: Peak at least this many × the event minimum: a warning (MEASUREMENTS.txt).
WARNING_FACTOR = 2.0
#: Peak at least this many × the event minimum: critical (MEASUREMENTS.txt).
CRITICAL_FACTOR = 5.0
#: The namespace of finding ids: re-processing the same flight gives the same
#: id to the same stretch, so an upload replaces instead of duplicating.
FINDINGS_NAMESPACE = uuid.UUID("6f1c2a8e-3b7d-4e55-9a51-0d2f7c4b8e10")

RANK: dict[Severity, int] = {"info": 0, "warning": 1, "critical": 2}
SENSOR_COLUMNS = ("raw_temp", "corrected_temp", "station_pressure_hpa")


def _symbol(unit: str) -> str:
    return {"C": "°C", "F": "°F"}.get(unit, unit)


def _minimum(event: Event, features: Mapping[str, float]) -> float:
    """The event minimum in the event's own unit (a difference: × 9/5 for °F)."""
    if event.signal == "pressure":
        return features.get("pressure_event_minimum_hpa", MIN_EVENT_HPA)
    return features.get("temp_event_minimum_c", MIN_EVENT_C) * (9 / 5 if event.unit == "F"
                                                                else 1.0)


def _clock(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60}:{s % 60:02d}"


def severity(event: Event, features: Mapping[str, float],
             height_measured: bool = True) -> Severity:
    if event.signal == "pressure" and not height_measured:
        return "info"
    # The tolerance lets an exact boundary count as reached in either unit
    # (2.88 °F / 1.44 °F is 1.9999999… in floating point).
    size = abs(event.peak) / _minimum(event, features) + 1e-9
    if size >= CRITICAL_FACTOR:
        return "critical"
    if size >= WARNING_FACTOR:
        return "warning"
    return "info"


def finding_id(flight_id: str, event: Event) -> str:
    return str(uuid.uuid5(FINDINGS_NAMESPACE,
                          f"{flight_id}:{event.signal}:{event.start_index}:{event.end_index}"))


def _where(event: Event, ground: bool = False) -> str:
    if ground:
        if event.x_m is not None and event.y_m is not None:
            return f"on the ground at ({event.x_m:.2f}, {event.y_m:.2f}) m"
        return "on the ground (position not measured)"
    points = event.point_ids
    if len(points) == 1:
        near = f"near {points[0]}"
    elif points:
        near = f"from {points[0]} to {points[-1]}"
    else:
        near = "between inspection points"
    if event.x_m is not None and event.y_m is not None and event.z_m is not None:
        return f"{near}, at ({event.x_m:.2f}, {event.y_m:.2f}) m, {event.z_m:.2f} m up"
    return f"{near} (position not measured)"


def _title(event: Event, ground: bool = False) -> str:
    place = " on the ground" if ground else (
        f" near {event.point_ids[0]}" if len(event.point_ids) == 1 else (
            " between points" if not event.point_ids else f" at {', '.join(event.point_ids)}"))
    if event.signal == "temperature":
        return ("Warmer" if event.direction == "rise" else "Cooler") + " than expected" + place
    return "Pressure " + ("higher" if event.direction == "rise" else "lower") + \
        " than expected" + place


#: Past this many noise σ, "N× its noise" reads as a number nobody can
#: picture (a sensor with 0.009 °C of noise makes a 4 °C patch "436×").
FAR_BEYOND_NOISE = 100


def _beyond_noise(z: float) -> str:
    return "far beyond its noise" if z >= FAR_BEYOND_NOISE else f"{z:.0f}× its noise"


def _sentence(event: Event, height_measured: bool, ground: bool = False) -> str:
    sym = _symbol(event.unit)
    word = "above" if event.direction == "rise" else "below"
    held = event.t_end_s - event.t_start_s
    when = (f"From {_clock(event.t_start_s)} to {_clock(event.t_end_s)} into the session, "
            f"{_where(event, True)}") if ground else \
        f"From {_clock(event.t_start_s)} to {_clock(event.t_end_s)}, {_where(event)}"
    if event.signal == "temperature":
        trend = ("its own trend there (the board warming or settling)" if ground
                 else "the drone's normal cooling curve")
        what = (f"the temperature sensor read {abs(event.peak):.1f} {sym} {word} {trend} at "
                f"its peak ({abs(event.delta):.1f} {sym} on average, "
                f"{_beyond_noise(event.z)})")
    else:
        corrected = "as measured (the drone was on the ground)" if ground else (
            "corrected for height" if height_measured else
            "NOT corrected for height (not measured — a climb would look the same)")
        what = (f"the station pressure, {corrected}, was {abs(event.peak):.2f} {sym} {word} "
                f"its trend at its peak ({abs(event.delta):.2f} {sym} on average, "
                f"{_beyond_noise(event.z)})")
    return f"{when}, {what}, for {held:.0f} s."


def _height_measured(tracks: Sequence[Track]) -> bool:
    return not any(t.signal == "pressure" and "height not measured" in t.model for t in tracks)


class FindingInterpreter:
    name = "findings"
    version = "1"

    def interpret(self, data: PointData, clean: CleanResult, classified: ClassifyResult,
                  ctx: FlightContext) -> InterpretResult:
        measured = _height_measured(classified.tracks)
        ground = ctx.scope == "session"
        findings = []
        for event in classified.events:
            height_ok = measured or event.signal != "pressure"
            findings.append(Finding(
                id=finding_id(ctx.flight_id, event), signal=event.signal,
                severity=severity(event, classified.features, height_ok),
                title=_title(event, ground), sentence=_sentence(event, height_ok, ground),
                start_index=event.start_index, end_index=event.end_index,
                t_start_s=event.t_start_s, t_end_s=event.t_end_s, unit=event.unit,
                observed=event.observed, expected=event.expected, delta=event.delta,
                z=event.z, point_ids=event.point_ids, x_m=event.x_m, y_m=event.y_m,
                z_m=event.z_m))
        points = ctx.points or (data.point,)
        whole = not ctx.points
        return InterpretResult(
            points=tuple(_verdict(p, data, clean, classified, findings, whole=whole,
                                  ground=ground) for p in points),
            findings=tuple(findings))


def _verdict(point: InspectionPoint, data: PointData, clean: CleanResult,
             classified: ClassifyResult, findings: Sequence[Finding], *,
             whole: bool, ground: bool = False) -> PointResult:
    def mine(point_id: str | None) -> bool:
        return whole or point_id == point.id

    readings = [r for r in data.readings if mine(r.point_id)]
    usable = [r for r in readings
              if any(c in r.values and clean.usable(r.index, c) for c in SENSOR_COLUMNS)]
    if not usable:
        return PointResult(point.id, "insufficient_data",
                           ("No usable readings at this point.",))
    here = [f for f in findings if whole or point.id in f.point_ids]
    serious = [f for f in here if RANK[f.severity] >= RANK["warning"]]
    if serious:
        return PointResult(
            point.id, "anomaly", tuple(f.sentence for f in serious),
            tuple(Alert(point.id, f.severity, f.sentence,
                        evidence_readings=tuple(r.index for r in usable
                                                if f.start_index <= r.index <= f.end_index))
                  for f in serious))
    if not classified.tracks:
        why = classified.sensors.reason or "the readings could not be analysed"
        return PointResult(point.id, "insufficient_data", (f"Not analysed: {why}.",))
    reasons = ["Nothing on the ground departed from the sensor's own trend." if ground else
               "Nothing here departed from the drone's normal cooling or the pressure trend."]
    reasons += [f"Slight: {f.sentence}" for f in here]
    return PointResult(point.id, "normal", tuple(reasons))
