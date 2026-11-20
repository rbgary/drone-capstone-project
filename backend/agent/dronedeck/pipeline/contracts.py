"""The contract every pipeline stage builds against — version 3.

The same types as docs/handoffs/sprint-1/done/dpp-contract.txt — this file is the
authority. Change a type here and every stage owner's code is affected, so a
change is agreed first and the contract doc updated in the same commit.

THREE STAGES, THE SENSORS ONLY (v3, 2026-11-15). clean → classify →
interpret. The drone carries no camera (docs/features/architecture.txt,
DECISIONS), so v2's frames, its enhance stage (clahe@1), the scene measure
(scene@1) and every image field of a finding are gone. Photos taken by hand
are classified on their own, outside this pipeline. A flight is processed once
its data has arrived — there is no live run while it flies.

THE UNIT OF WORK IS THE FLIGHT (v2, 2026-10-09). Clean and classify run once
over the whole flight, handed as one PointData whose point is WHOLE_FLIGHT;
every reading carries the inspection point it was taken at (point_id), so the
points are a view, not a split. Why: an anomaly is a BLOCK of readings that
departs from what was expected, and a block can span two points or the transit
between them — which a stage that only ever sees one point's readings cannot
find (v1 analysed transit readings not at all).

UNITS: metres, seconds, hPa. Temperatures are in `FlightContext.temp_unit` —
whatever the operator chose at launch — and a stage converts them itself
(an absolute temperature × 9/5 + 32, a difference × 9/5 only).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

#: A flight flown without a mission is one inspection point with this id, and
#: the whole flight handed to a stage carries it too.
WHOLE_FLIGHT = "flight"
#: A session processed as a whole (its one-a-second readings, on the ground and
#: between flights) is one point with this id.
WHOLE_SESSION = "session"
#: What a run covers: one flight (10 readings a second, airborne), or the
#: session around its flights (1 a second, the drone on the ground).
Scope = Literal["flight", "session"]


class StageError(RuntimeError):
    """A stage cannot run at all (e.g. its model file is missing). The runner
    records the reason and carries on without that stage's output."""


# ── shared input ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InspectionPoint:
    id: str                                 # stable id from the mission; "flight" if none
    label: str | None
    x_m: float                              # Lighthouse metres (absolute)
    y_m: float
    z_m: float                              # metres above the floor captured at takeoff


@dataclass(frozen=True)
class FlightContext:
    flight_id: str
    session_id: str | None
    temp_unit: Literal["C", "F"]            # the unit of EVERY temperature value
    ground_z_m: float | None                # the floor's Lighthouse z at takeoff
    started_at: datetime                    # UTC
    workdir: Path                           # the ONLY place a stage may write files
    #: The inspection points of the mission flown, in order; () when the
    #: flight flew no mission.
    points: tuple[InspectionPoint, ...] = ()
    #: "session": flight_id holds the SESSION's id, the readings are its
    #: one-a-second samples, and only the stretches on the ground are analysed
    #: for events (the flights have results of their own).
    scope: Scope = "flight"
    #: Seconds between readings when nothing is lost: 0.1 for a flight, 1.0 for
    #: a session. The cleaner's gap limit is measured in these.
    period_s: float = 0.1


@dataclass(frozen=True)
class Reading:
    """One row of the flight CSV. `values` holds its numeric columns by the
    field names of TelemetryRow (telemetry/row.py); None = not recorded."""

    index: int
    recorded_at: datetime
    t_s: float                              # seconds since the flight's first row
    values: Mapping[str, float | None]
    #: The inspection point being held when it was taken; None in transit.
    point_id: str | None = None
    #: The row's words: mode, thermal_state, event (the correction engine's
    #: state — the cleaner and classifier need to know when it switched).
    text: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PointData:
    point: InspectionPoint                  # WHOLE_FLIGHT for the whole flight
    readings: tuple[Reading, ...]           # ordered by index


# ── stage 1: clean (story 4.2) ───────────────────────────────────────────

FlagKind = Literal[
    "missing",          # None or NaN
    "out_of_range",     # outside what the sensor can measure at all
    "spike",            # one value off its neighbours
    "stuck",            # a value that stopped updating
    "gap",              # time lost before this row (column None)
    "implausible",      # a change between two readings no drone can make
    "untrusted",        # a position the drone did not measure (no base station)
]


@dataclass(frozen=True)
class ReadingFlag:
    index: int                              # Reading.index
    column: str | None                      # None = the whole row (e.g. a gap)
    kind: FlagKind
    reason: str                             # one sentence an inspector can read


@dataclass(frozen=True)
class CleanResult:
    readings: tuple[Reading, ...]           # EXACTLY the input readings, unchanged
    flags: tuple[ReadingFlag, ...]
    _bad: frozenset[tuple[int, str | None]] = field(default=frozenset(), repr=False,
                                                     compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_bad", frozenset((f.index, f.column) for f in self.flags))

    def usable(self, index: int, column: str) -> bool:
        """False when that value, or its whole row, was flagged."""
        return (index, column) not in self._bad and (index, None) not in self._bad


class Cleaner(Protocol):
    name: str
    version: str

    def clean(self, data: PointData, ctx: FlightContext) -> CleanResult: ...


# ── stage 2: classify (story 4.6) ────────────────────────────────────────

LabelValue = Literal["normal", "faulty", "unknown"]
Signal = Literal["temperature", "pressure"]


@dataclass(frozen=True)
class Label:
    value: LabelValue
    p_faulty: float | None                  # 0..1 from the model; None when unknown
    model: str                              # "<name>@<version>"
    reason: str | None = None               # REQUIRED when value is "unknown"

    def __post_init__(self) -> None:
        if self.value == "unknown" and not self.reason:
            raise ValueError("an 'unknown' label must say why")
        if self.p_faulty is not None and not 0.0 <= self.p_faulty <= 1.0:
            raise ValueError("p_faulty must be between 0 and 1")


@dataclass(frozen=True)
class Track:
    """One analysed signal, reading by reading: what was measured (after any
    correction, e.g. for height) and what was expected. The web draws both."""

    signal: Signal
    column: str                             # the CSV column it was read from
    unit: str                               # "C", "F" or "hPa"
    model: str                              # e.g. "cooling-curve", "linear"
    noise: float | None                     # σ of the residual, in `unit`
    indexes: tuple[int, ...]                # Reading.index of each value below
    observed: tuple[float, ...]
    expected: tuple[float, ...]


@dataclass(frozen=True)
class Segment:
    """A block: consecutive readings whose residual holds one level."""

    signal: Signal
    start_index: int                        # Reading.index, inclusive
    end_index: int                          # inclusive
    t_start_s: float
    t_end_s: float
    readings: int
    mean_residual: float                    # in the track's unit


@dataclass(frozen=True)
class Event:
    """A stretch the classifier says departs from what was expected: one block,
    or adjacent blocks that depart the same way (a ramp up and down is a
    staircase of blocks, and one event)."""

    signal: Signal
    unit: str
    start_index: int
    end_index: int
    t_start_s: float
    t_end_s: float
    direction: Literal["rise", "drop"]
    observed: float                         # the block's mean
    expected: float                         # what was expected over it
    delta: float                            # observed − expected, over the event
    z: float                                # |delta| in noise units
    peak: float                             # the largest block's departure, signed
    slope_per_s: float                      # within the block
    point_ids: tuple[str, ...]              # inspection points it overlaps
    x_m: float | None                       # mean position, when measured
    y_m: float | None
    z_m: float | None


@dataclass(frozen=True)
class ClassifyResult:
    sensors: Label                          # one label for the readings
    features: Mapping[str, float] = field(default_factory=dict)  # names carry units
    tracks: tuple[Track, ...] = ()
    segments: tuple[Segment, ...] = ()
    events: tuple[Event, ...] = ()


class Classifier(Protocol):
    name: str
    version: str

    def classify(self, data: PointData, clean: CleanResult,
                 ctx: FlightContext) -> ClassifyResult: ...


# ── stage 3: interpret (Samuel) ──────────────────────────────────────────

Verdict = Literal["normal", "anomaly", "insufficient_data"]
Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class Alert:
    point_id: str
    severity: Severity
    reason: str
    evidence_readings: tuple[int, ...] = ()


@dataclass(frozen=True)
class PointResult:
    point_id: str
    verdict: Verdict
    reasons: tuple[str, ...]
    alerts: tuple[Alert, ...] = ()


@dataclass(frozen=True)
class Finding:
    """An event judged: how serious, in words, where and when. The readings
    are the evidence."""

    id: str                                 # deterministic: re-processing upserts
    signal: Signal
    severity: Severity
    title: str
    sentence: str
    start_index: int
    end_index: int
    t_start_s: float
    t_end_s: float
    unit: str
    observed: float
    expected: float
    delta: float
    z: float
    point_ids: tuple[str, ...]
    x_m: float | None
    y_m: float | None
    z_m: float | None


@dataclass(frozen=True)
class InterpretResult:
    points: tuple[PointResult, ...]         # one per inspection point, in order
    findings: tuple[Finding, ...] = ()


class Interpreter(Protocol):
    name: str
    version: str

    def interpret(self, data: PointData, clean: CleanResult, classified: ClassifyResult,
                  ctx: FlightContext) -> InterpretResult: ...


# ── the flight's result ──────────────────────────────────────────────────


@dataclass(frozen=True)
class StageFailure:
    point_id: str
    stage: str
    reason: str


@dataclass(frozen=True)
class FlightResult:
    flight_id: str
    pipeline_version: str
    stages: Mapping[str, str]                # stage → "name@version"
    created_at: str
    points: tuple[PointResult, ...]
    failures: tuple[StageFailure, ...] = ()
    #: Per point: how many readings and flags it had — so a verdict can be
    #: judged against how much evidence it rested on.
    summary: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    session_id: str | None = None
    temp_unit: str = "C"
    findings: tuple[Finding, ...] = ()
    flags: tuple[ReadingFlag, ...] = ()
    tracks: tuple[Track, ...] = ()
    segments: tuple[Segment, ...] = ()
    #: "session": flight_id is the session's id (see FlightContext.scope).
    scope: Scope = "flight"
