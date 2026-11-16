"""A session processed as a whole — its one-a-second samples around its flights
(the owner, 2026-10-09: "as long as a session is started we process whatever
data comes, not only data in flight").

Synthetic sessions, built here, so each rule is checked on numbers we chose:
the cleaner at 1 Hz, the ground stretches, the ground classifier, the source,
the words, and `dronedeck process --session` / `--all`.
"""

from __future__ import annotations

import csv
import json
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dronedeck.pipeline import PIPELINE_VERSION
from dronedeck.pipeline.compose import session_stages
from dronedeck.pipeline.contracts import (
    WHOLE_SESSION,
    FlightContext,
    InspectionPoint,
    PointData,
    Reading,
)
from dronedeck.pipeline.runner import run_session
from dronedeck.pipeline.sinks import LocalResultSink
from dronedeck.pipeline.sources import LocalSessionSource, SessionNotFound
from dronedeck.pipeline.stages.classify import ground
from dronedeck.pipeline.stages.clean.robust import RobustCleaner

T0 = datetime(2026, 10, 9, 18, 0, 0, tzinfo=UTC)
SID = "5e550000-0000-4000-8000-000000000001"
FIELDS = ["recorded_at", "mode", "height_m", "baro.temp", "baro.pressure", "stateEstimate.x",
          "stateEstimate.y", "stateEstimate.z", "pm.vbat", "stabilizer.thrust",
          "lighthouse.bsReceive"]


def write_session(root: Path, *, seconds: int = 300, flights=(), bump=None, gap_at=None,
                  seed: int = 0, session_id: str = SID) -> Path:
    """A session folder: samples.csv at 1 Hz (a board warming towards 36 °C,
    pressure drifting slowly) and meta.json with its flights.
    `flights` are (start s, end s); `bump` is (start s, size °C) — 10 s up,
    20 s held, 10 s down; `gap_at` drops 4 samples there."""
    rng = random.Random(seed)
    folder = root / "sessions" / session_id
    folder.mkdir(parents=True)
    rows = []
    for k in range(seconds):
        if gap_at is not None and gap_at <= k < gap_at + 4:
            continue
        flying = any(a <= k <= b for a, b in flights)
        temp = 36.0 - 4.0 * math.exp(-k / 120.0) + rng.gauss(0, 0.02)
        if flying:
            temp -= 3.0                                  # the propellers' air
        if bump is not None:
            start, size = bump
            j = k - start
            if 0 <= j < 40:
                temp += size * min(j / 10, 1.0, (39 - j) / 10)
        rows.append({
            "recorded_at": (T0 + timedelta(seconds=k)).isoformat(), "mode": "manual",
            "height_m": "0.40" if flying else "",
            "baro.temp": f"{temp:.4f}",
            "baro.pressure": f"{1013.2 + 0.0005 * k + rng.gauss(0, 0.01):.4f}",
            "stateEstimate.x": "0.10", "stateEstimate.y": "0.20", "stateEstimate.z": "1.00",
            "pm.vbat": f"{4.1 - 0.0005 * k:.3f}",
            "stabilizer.thrust": "40000" if flying else "0",
            "lighthouse.bsReceive": "1",
        })
    with (folder / "samples.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    meta = {"id": session_id, "started_at": T0.isoformat(),
            "ended_at": (T0 + timedelta(seconds=seconds)).isoformat(),
            "flights": [{"id": f"f{n}", "mode": "manual",
                         "started_at": (T0 + timedelta(seconds=a)).isoformat(),
                         "ended_at": (T0 + timedelta(seconds=b)).isoformat()}
                        for n, (a, b) in enumerate(flights)]}
    (folder / "meta.json").write_text(json.dumps(meta))
    return folder


def _ctx(tmp_path: Path, period: float = 1.0) -> FlightContext:
    return FlightContext(SID, SID, "C", None, T0, tmp_path, scope="session", period_s=period)


def _readings(n: int, dt: float, gap_after: int | None = None) -> tuple[Reading, ...]:
    out, t = [], 0.0
    for i in range(n):
        if gap_after is not None and i == gap_after:
            t += 3.0
        out.append(Reading(i + 1, T0 + timedelta(seconds=t), t,
                           {"raw_temp": 30.0 + 0.001 * i, "station_pressure_hpa": 1013.0}))
        t += dt
    return tuple(out)


class TestTheCleanerAtOneHertz:
    def test_one_reading_a_second_is_not_a_gap(self, tmp_path):
        data = PointData(InspectionPoint(WHOLE_SESSION, None, 0, 0, 0), _readings(120, 1.0))
        flags = RobustCleaner().clean(data, _ctx(tmp_path)).flags
        assert not [f for f in flags if f.kind == "gap"]

    def test_three_seconds_lost_at_one_hertz_is_a_gap(self, tmp_path):
        data = PointData(InspectionPoint(WHOLE_SESSION, None, 0, 0, 0),
                         _readings(120, 1.0, gap_after=60))
        gaps = [f for f in RobustCleaner().clean(data, _ctx(tmp_path)).flags if f.kind == "gap"]
        assert [f.index for f in gaps] == [61]
        assert "limit 2.50 s" in gaps[0].reason

    def test_the_same_readings_at_flight_rate_are_all_gaps(self, tmp_path):
        # The flight's 10 Hz limit is untouched: a 1 s cadence there is lost time.
        data = PointData(InspectionPoint(WHOLE_SESSION, None, 0, 0, 0), _readings(20, 1.0))
        flags = RobustCleaner().clean(data, _ctx(tmp_path, period=0.1)).flags
        assert len([f for f in flags if f.kind == "gap"]) == 19


    def test_a_stuck_sensor_is_said_in_seconds_at_one_hertz(self, tmp_path):
        readings = tuple(
            Reading(i + 1, T0 + timedelta(seconds=i), float(i),
                    {"raw_temp": 30.0 + 0.01 * i,
                     "station_pressure_hpa": 1013.25 if 40 <= i < 54 else 1013.0 + 0.003 * i})
            for i in range(120))
        data = PointData(InspectionPoint(WHOLE_SESSION, None, 0, 0, 0), readings)
        stuck = [f for f in RobustCleaner().clean(data, _ctx(tmp_path)).flags if f.kind == "stuck"]
        assert len(stuck) == 14
        assert "14 readings (14.0 s" in stuck[0].reason


class TestGroundStretches:
    def test_flights_split_the_session_and_their_edges_are_left_out(self, tmp_path):
        s = LocalSessionSource(write_session(tmp_path, seconds=300, flights=[(100, 160)])
                               .parent.parent).load(SID)
        stretches = ground.ground_stretches(s.whole.readings)
        spans = [(s.whole.readings[int(x[0])].t_s, s.whole.readings[int(x[-1])].t_s)
                 for x in stretches]
        assert len(spans) == 2
        before, after = spans
        assert before[0] == 0 and before[1] <= 99 - ground.GROUND_SETTLE_S + 1
        assert after[0] >= 161 + ground.GROUND_SETTLE_S - 1 and after[1] == 299


class TestTheGroundClassifier:
    def _run(self, tmp_path, **kw):
        root = write_session(tmp_path, **kw).parent.parent
        return run_session(SID, source=LocalSessionSource(root),
                           sink=LocalResultSink(tmp_path / "out"), stages=session_stages())[0]

    def test_a_board_warming_on_the_ground_is_normal(self, tmp_path):
        result = self._run(tmp_path, seconds=400, flights=[(150, 200)])
        assert result.scope == "session" and result.flight_id == SID
        assert result.findings == ()
        assert result.points[0].point_id == WHOLE_SESSION
        assert result.points[0].verdict == "normal"
        assert "on the ground" in result.points[0].reasons[0]
        model = next(t.model for t in result.tracks if t.signal == "temperature")
        assert "on the ground" in model

    def test_something_warm_beside_it_is_found_and_said_as_ground(self, tmp_path):
        result = self._run(tmp_path, seconds=400, bump=(250, 3.0))
        assert len(result.findings) == 1
        f = result.findings[0]
        assert f.signal == "temperature" and f.delta > 0
        assert f.start_index >= 250 and f.end_index <= 300
        assert f.title == "Warmer than expected on the ground"
        assert "on the ground" in f.sentence and "into the session" in f.sentence
        assert "cooling curve" not in f.sentence and " m up" not in f.sentence
        assert f.point_ids == ()

    def test_the_flights_cooling_is_never_a_ground_event(self, tmp_path):
        result = self._run(tmp_path, seconds=400, flights=[(100, 220)])
        assert result.findings == ()

    def test_a_session_too_short_to_judge_says_so(self, tmp_path):
        result = self._run(tmp_path, seconds=20)
        assert result.points[0].verdict == "insufficient_data"


class TestTheSource:
    def test_samples_read_as_the_flight_csvs_columns(self, tmp_path):
        root = write_session(tmp_path, seconds=30, flights=[(10, 15)]).parent.parent
        s = LocalSessionSource(root).load(SID)
        first = s.whole.readings[0]
        assert first.index == 1 and first.t_s == 0
        assert set(first.values) >= {"raw_temp", "station_pressure_hpa", "x_m", "y_m", "z_m",
                                     "battery_v", "thrust", "lighthouse_received"}
        phases = [r.text["phase"] for r in s.whole.readings]
        assert phases[10] == "flying" and phases[5] == "ground" and phases[20] == "ground"

    def test_a_session_with_no_samples_is_not_found(self, tmp_path):
        (tmp_path / "sessions" / SID).mkdir(parents=True)
        with pytest.raises(SessionNotFound):
            LocalSessionSource(tmp_path).load(SID)


class TestTheCommand:
    @pytest.fixture
    def data(self, tmp_path, monkeypatch):
        root = tmp_path / "data"
        write_session(root, seconds=200)
        monkeypatch.setenv("DRONEDECK_DATA_DIR", str(root))
        return root

    def test_process_session_saves_a_session_result_and_records_it(self, data, capsys):
        from dronedeck import cli, paths

        assert cli.main(["process", "--session", SID]) == 0
        out = json.loads((paths.results_dir() / "sessions" / SID / "result.json").read_text())
        assert out["scope"] == "session" and out["pipeline_version"] == PIPELINE_VERSION
        assert "frames" not in out
        assert json.loads((data / "sessions" / SID / "meta.json").read_text())["processing"] \
            == "done"

    def test_all_processes_sessions_too_and_only_once(self, data, capsys):
        from dronedeck import cli

        cli.main(["process", "--all"])
        assert "processed 1 session(s)" in capsys.readouterr().out
        cli.main(["process", "--all"])
        assert "processed 0 session(s)" in capsys.readouterr().out

    def test_a_bad_id_is_refused(self, data, capsys):
        from dronedeck import cli

        assert cli.main(["process", "--session", "../etc"]) == 2
