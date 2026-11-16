"""Interpret: the classifier's labels → a verdict per inspection point.

THIS HOLDS NO TEMPERATURE OR PRESSURE THRESHOLD, and that is deliberate. A
"normal operating" limit is measured on the equipment (story 4.6: "define
normal thresholds"), never guessed — CLAUDE.md, flight invariant 1, and the
3.70 V threshold that refused good flights. The verdict rests on the
classifier alone:

    no usable readings                    insufficient_data
    the sensor label "unknown"            insufficient_data — and why
    the sensor label "faulty"             anomaly, with an alert quoting the
                                          evidence (reading indexes)
    otherwise                             normal

Contract v3: it is handed the whole flight and gives each inspection point its
verdict from the readings stamped with that point. It makes no
findings — the finding interpreter does (stages/interpret/findings.py).
"""

from __future__ import annotations

from dronedeck.pipeline.contracts import (
    Alert,
    ClassifyResult,
    CleanResult,
    FlightContext,
    InspectionPoint,
    InterpretResult,
    PointData,
    PointResult,
)


class LabelInterpreter:
    name = "labels"
    version = "3"

    def interpret(self, data: PointData, clean: CleanResult, classified: ClassifyResult,
                  ctx: FlightContext) -> InterpretResult:
        points = ctx.points or (data.point,)
        return InterpretResult(tuple(self._point(p, data, clean, classified, whole=not ctx.points)
                                     for p in points))

    @staticmethod
    def _point(point: InspectionPoint, data: PointData, clean: CleanResult,
               classified: ClassifyResult, *, whole: bool) -> PointResult:
        def mine(point_id: str | None) -> bool:
            return whole or point_id == point.id

        readings = [r for r in clean.readings if mine(r.point_id)]
        usable = [r for r in readings if any(clean.usable(r.index, c) for c in r.values)]
        if not usable:
            return PointResult(point.id, "insufficient_data",
                               ("No usable readings at this point.",))

        # The sensor label is the flight's. With events, it is this point's
        # only where an event overlaps the point's readings.
        mine_events = [e for e in classified.events if whole or point.id in e.point_ids]
        sensors_faulty = classified.sensors.value == "faulty" and (
            bool(mine_events) or not classified.events)
        if sensors_faulty:
            shown = ", ".join(f"{k} {v:.2f}" for k, v in classified.features.items())
            reason = (f"The sensor model ({classified.sensors.model}) labelled the "
                      f"readings faulty" + (f": {shown}." if shown else "."))
            evidence = tuple(r.index for r in usable if any(
                e.start_index <= r.index <= e.end_index for e in mine_events)) or \
                tuple(r.index for r in usable)
            return PointResult(point.id, "anomaly", (reason,),
                               (Alert(point.id, "warning", reason, evidence_readings=evidence),))

        if classified.sensors.value == "unknown":
            why = classified.sensors.reason or "no reason given"
            return PointResult(point.id, "insufficient_data",
                               (f"No model gave a verdict: {why}.",))
        return PointResult(point.id, "normal", ("Nothing at this point was labelled faulty.",))
