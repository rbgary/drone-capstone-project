"""Loading a flight, running the stages once over the whole flight (contract v3),
a verdict per inspection point, surviving a stage that fails or breaks the
contract, and saving the result."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dronedeck.cli import main
from dronedeck.pipeline.compose import default_stages
from dronedeck.pipeline.contracts import (
    CleanResult,
    InterpretResult,
    PointResult,
    ReadingFlag,
    StageError,
)
from dronedeck.pipeline.runner import run_flight
from dronedeck.pipeline.sinks import LocalResultSink
from dronedeck.pipeline.sources import FlightNotFound, LocalFlightSource

FIXTURE = Path(__file__).parent / "fixtures" / "data"
FLIGHT = "372bbdc4-d323-42bb-9e6c-29ef02e3794c"


def run(tmp_path, stages=None):
    return run_flight(FLIGHT, source=LocalFlightSource(FIXTURE),
                      sink=LocalResultSink(tmp_path), stages=stages or default_stages())


class TestLoading:
    def test_readings_group_by_inspection_point(self):
        flight = LocalFlightSource(FIXTURE).load(FLIGHT)
        assert [p.point.id for p in flight.points] == ["P1", "P2", "P3"]
        assert [len(p.readings) for p in flight.points] == [60, 60, 0]
        assert flight.unassigned_readings == 400 - 120
        assert flight.temp_unit == "C"

    def test_readings_keep_their_numbers_their_time_and_their_point(self):
        first = LocalFlightSource(FIXTURE).load(FLIGHT).points[0].readings[0]
        assert first.index == 100
        assert isinstance(first.values["corrected_temp"], float)
        assert "temp_unit" not in first.values and "point_id" not in first.values
        assert first.point_id == "P1"
        assert first.text["thermal_state"].startswith("FLIGHT")
        assert set(first.text) <= {"mode", "thermal_state", "event"}
        assert first.t_s > 0

    def test_the_whole_flight_holds_every_reading_transit_included(self):
        flight = LocalFlightSource(FIXTURE).load(FLIGHT)
        assert flight.whole.point.id == "flight"
        assert len(flight.whole.readings) == 400
        assert [r.index for r in flight.whole.readings] == list(range(400))
        assert sum(1 for r in flight.whole.readings if r.point_id is None) == 280
        assert [p.id for p in flight.plan] == ["P1", "P2", "P3"]

    def test_a_flight_with_no_mission_is_one_point(self, tmp_path):
        import shutil
        folder = tmp_path / "flights" / "2026-09-24"
        folder.mkdir(parents=True)
        for csv_path in (FIXTURE / "flights" / "2026-09-24").glob("*.csv"):
            shutil.copy(csv_path, folder)
        flight = LocalFlightSource(tmp_path).load(FLIGHT)
        assert flight.plan == () and flight.points == (flight.whole,)
        assert flight.unassigned_readings == 0

    def test_a_flight_that_is_not_here_says_so(self):
        with pytest.raises(FlightNotFound):
            LocalFlightSource(FIXTURE).load("00000000-0000-0000-0000-000000000000")


class TestRunning:
    def test_the_whole_flight_gets_a_result_per_point(self, tmp_path):
        result, where = run(tmp_path)
        assert [p.point_id for p in result.points] == ["P1", "P2", "P3"]
        # A real flight's readings: nothing departs from expected at P1 and P2;
        # P3 was never reached.
        assert [p.verdict for p in result.points] == ["normal", "normal", "insufficient_data"]
        assert result.stages == {"clean": "robust@1", "classify": "blocks@1",
                                 "interpret": "findings@1"}
        assert result.pipeline_version == "4"
        saved = json.loads(Path(where).read_text())
        assert saved["flight_id"] == FLIGHT
        assert saved["summary"]["_transit"] == {"readings": 280}
        assert saved["summary"]["P1"]["readings"] == 60
        assert saved["session_id"] == "a5788fe1-02fb-49d7-ac1c-b400d825d45f"
        assert "frames" not in saved and "frames" not in saved["summary"]["P1"]

    def test_running_again_replaces_the_result(self, tmp_path):
        run(tmp_path)
        _, where = run(tmp_path)
        assert sorted(p.name for p in Path(where).parent.iterdir()) == ["result.json", "work"]

    def test_a_warm_patch_is_a_finding_and_its_point_an_anomaly(self, tmp_path):
        """End to end on the real fixture flight: a warm patch planted at P1's
        readings is found, judged, put into words and pinned to P1."""
        import csv
        import shutil
        root = tmp_path / "data"
        shutil.copytree(FIXTURE, root)
        (path,) = (root / "flights" / "2026-09-24").glob("*.csv")
        with path.open(newline="") as f:
            rows = list(csv.DictReader(f))
        for k, row in enumerate(rows[100:160]):              # P1's readings
            lift = 4.0 * min(k / 15, 1.0, (59 - k) / 15)
            for column in ("raw_temp", "corrected_temp"):
                row[column] = repr(float(row[column]) + lift)
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        result, _ = run_flight(FLIGHT, source=LocalFlightSource(root),
                               sink=LocalResultSink(tmp_path / "out"), stages=default_stages())
        (finding,) = [f for f in result.findings if f.signal == "temperature"]
        assert finding.point_ids == ("P1",) and finding.severity in ("warning", "critical")
        assert finding.sentence.startswith("From ") and "near P1" in finding.sentence
        assert result.points[0].verdict == "anomaly"

    def test_a_failing_stage_is_recorded_and_the_flight_still_finishes(self, tmp_path):
        class Broken:
            name, version = "broken", "1"

            def classify(self, *args):
                raise StageError("the model file is missing")

        result, _ = run(tmp_path, replace(default_stages(), classifier=Broken()))
        assert {(f.point_id, f.stage) for f in result.failures} == {("flight", "classify")}
        assert "model file is missing" in result.failures[0].reason
        assert len(result.points) == 3

    def test_a_cleaner_that_edits_readings_breaks_the_contract(self, tmp_path):
        class Editor:
            name, version = "editor", "1"

            def clean(self, data, ctx):
                return CleanResult(data.readings[1:], ())

        result, _ = run(tmp_path, replace(default_stages(), cleaner=Editor()))
        assert result.points[0].verdict == "insufficient_data"
        assert "may only flag" in result.failures[0].reason

    def test_an_interpreter_that_skips_a_point_breaks_the_contract(self, tmp_path):
        class Skips:
            name, version = "skips", "1"

            def interpret(self, data, clean, classified, ctx):
                return InterpretResult((PointResult("P1", "normal", ("fine",)),))

        result, _ = run(tmp_path, replace(default_stages(), interpreter=Skips()))
        assert [p.point_id for p in result.points] == ["P1", "P2", "P3"]
        assert all(p.verdict == "insufficient_data" for p in result.points)
        assert "every inspection point" in result.failures[0].reason

    def test_the_stages_see_the_whole_flight_once(self, tmp_path):
        seen = []

        class Counts:
            name, version = "counts", "1"

            def clean(self, data, ctx):
                seen.append((data.point.id, len(data.readings), [p.id for p in ctx.points]))
                return CleanResult(data.readings, ())

        run(tmp_path, replace(default_stages(), cleaner=Counts()))
        assert seen == [("flight", 400, ["P1", "P2", "P3"])]

    def test_a_flagged_reading_is_not_usable(self):
        data = LocalFlightSource(FIXTURE).load(FLIGHT).points[0]
        first = data.readings[0].index
        clean = CleanResult(data.readings, (
            ReadingFlag(first, "corrected_temp", "spike", "a planted spike"),))
        assert not clean.usable(first, "corrected_temp")
        assert clean.usable(first, "raw_temp")


class TestCommand:
    def test_process_fixture_runs_the_whole_pipeline(self, capsys):
        assert main(["process", "--fixture"]) == 0
        out = capsys.readouterr().out
        assert "P1" in out and "insufficient_data" in out and "saved" in out

    def test_process_an_unknown_flight_says_so(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
        assert main(["process", "--flight", "nope-nope"]) == 2
        assert "No readings" in capsys.readouterr().out
