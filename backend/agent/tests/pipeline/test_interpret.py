"""Interpret (stage 3), findings@1: events → findings with a severity and words,
and a verdict per inspection point. The telemetry is the evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dronedeck.pipeline.contracts import (
    ClassifyResult,
    CleanResult,
    Event,
    FlightContext,
    InspectionPoint,
    Label,
    PointData,
    Reading,
    Track,
)
from dronedeck.pipeline.stages.interpret.findings import (
    CRITICAL_FACTOR,
    WARNING_FACTOR,
    FindingInterpreter,
    finding_id,
    severity,
)

START = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
FEATURES = {"temp_event_minimum_c": 0.8, "pressure_event_minimum_hpa": 0.2}
POINTS = (InspectionPoint("P1", None, 0.5, 0.2, 0.4), InspectionPoint("P2", None, 1.0, 0.2, 0.4),
          InspectionPoint("P3", None, 1.5, 0.2, 0.4))


def event(peak: float = 3.0, *, signal: str = "temperature", unit: str = "C",
          points: tuple[str, ...] = ("P2",), placed: bool = True) -> Event:
    return Event(signal=signal, unit=unit, start_index=420, end_index=550,  # type: ignore[arg-type]
                 t_start_s=42.0, t_end_s=55.0, direction="rise" if peak > 0 else "drop",
                 observed=33.0, expected=31.1, delta=peak * 0.6, z=9.4, peak=peak,
                 slope_per_s=0.01, point_ids=points,
                 x_m=1.02 if placed else None, y_m=0.48 if placed else None,
                 z_m=0.41 if placed else None)


def flight_data() -> PointData:
    readings = []
    for i in range(700):
        t = i * 0.1
        pid = "P1" if 20 <= t < 30 else "P2" if 40 <= t < 60 else None
        readings.append(Reading(i, START + timedelta(seconds=t), t,
                                {"raw_temp": 30.0, "station_pressure_hpa": 1016.0},
                                point_id=pid))
    return PointData(InspectionPoint("flight", None, 0, 0, 0), tuple(readings))


def tracks(model: str = "linear") -> tuple[Track, ...]:
    return (Track("temperature", "raw_temp", "C", "cooling-curve", 0.02, (), (), ()),
            Track("pressure", "station_pressure_hpa", "hPa", model, 0.015, (), (), ()))


def run(events: tuple[Event, ...] = (), *, points=POINTS, model: str = "linear",
        fitted: bool = True):
    data = flight_data()
    classified = ClassifyResult(
        Label("faulty" if events else "normal", None, "blocks@1"), FEATURES,
        tracks=tracks(model) if fitted else (), events=events)
    ctx = FlightContext("flight-1", None, "C", None, START, Path("/tmp"), points=points)
    return FindingInterpreter().interpret(data, CleanResult(data.readings, ()),
                                          classified, ctx)


@pytest.mark.parametrize(("peak", "expected"), [
    (0.8, "info"), (0.8 * WARNING_FACTOR - 0.01, "info"), (0.8 * WARNING_FACTOR, "warning"),
    (0.8 * CRITICAL_FACTOR - 0.01, "warning"), (0.8 * CRITICAL_FACTOR, "critical"),
    (-0.8 * WARNING_FACTOR, "warning"),                       # cooler counts the same
])
def test_severity_is_the_peak_against_the_measured_minimum(peak, expected):
    assert severity(event(peak), FEATURES) == expected


def test_a_fahrenheit_minimum_is_a_difference():
    """0.8 °C is 1.44 °F as a difference — never 33.44 °F."""
    assert severity(event(0.8 * 9 / 5 * WARNING_FACTOR, unit="F"), FEATURES) == "warning"
    assert severity(event(0.8 * 9 / 5 * WARNING_FACTOR - 0.01, unit="F"), FEATURES) == "info"


def test_pressure_with_no_measured_height_is_never_more_than_info():
    big = event(5.0, signal="pressure", unit="hPa")
    assert severity(big, FEATURES) == "critical"
    assert severity(big, FEATURES, height_measured=False) == "info"
    (finding,) = run((big,), model="linear, height not measured").findings
    assert finding.severity == "info"
    assert "NOT corrected for height" in finding.sentence


def test_the_sentence_says_when_where_how_much_and_how_long():
    (finding,) = run((event(3.1),)).findings
    assert finding.sentence == (
        "From 0:42 to 0:55, near P2, at (1.02, 0.48) m, 0.41 m up, the temperature sensor "
        "read 3.1 °C above the drone's normal cooling curve at its peak (1.9 °C on average, "
        "9× its noise), for 13 s.")
    assert finding.title == "Warmer than expected near P2"


def test_a_huge_departure_is_far_beyond_the_noise_not_a_huge_number():
    (finding,) = run((replace(event(4.0), z=436.0),)).findings
    assert "far beyond its noise" in finding.sentence and "436" not in finding.sentence


def test_an_event_in_transit_with_no_position_says_both():
    (finding,) = run((event(-2.0, points=(), placed=False),)).findings
    assert "between inspection points (position not measured)" in finding.sentence
    assert "below" in finding.sentence and finding.title.startswith("Cooler")


def test_the_point_with_a_warning_is_an_anomaly_and_the_others_are_not():
    result = run((event(3.0, points=("P2",)),))
    p1, p2, p3 = result.points
    assert p1.verdict == "normal"
    assert p2.verdict == "anomaly" and p2.alerts[0].severity == "warning"
    assert p2.alerts[0].evidence_readings[0] >= 420
    assert p3.verdict == "insufficient_data"                 # never reached
    assert "No usable readings" in p3.reasons[0]


def test_an_info_finding_leaves_the_point_normal_and_says_so():
    (p1, p2, _) = run((event(1.0, points=("P2",)),)).points
    assert p2.verdict == "normal"
    assert any(r.startswith("Slight: ") for r in p2.reasons)


def test_a_flight_with_no_mission_is_one_point():
    result = run((event(3.0, points=()),), points=())
    (whole,) = result.points
    assert (whole.point_id, whole.verdict) == ("flight", "anomaly")


def test_nothing_fitted_is_insufficient_data_with_why():
    data_result = run((), fitted=False)
    assert all(p.verdict == "insufficient_data" for p in data_result.points[:2])


def test_finding_ids_are_stable_so_a_reprocess_replaces():
    a = finding_id("flight-1", event(3.0))
    assert a == finding_id("flight-1", event(4.0))          # same stretch, same id
    assert a != finding_id("flight-1", replace(event(3.0), end_index=551))
    assert a != finding_id("flight-2", event(3.0))
