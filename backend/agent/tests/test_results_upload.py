"""A processed flight reaches the web (sync/results.py, migration
20261009000016): its result and its findings — after the flight's own row,
replacing on re-process, retracting what a re-process no longer finds, and
waiting on disk when offline."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from dronedeck.cli import main
from dronedeck.sync import results
from dronedeck.sync.cloud import CloudError
from dronedeck.sync.outbox import Kind, Outbox
from dronedeck.sync.syncer import Syncer
from tests.test_sync import FakeCloud

FIXTURE = Path(__file__).parent / "pipeline" / "fixtures" / "data"
FLIGHT = "372bbdc4-d323-42bb-9e6c-29ef02e3794c"


class ResultsCloud(FakeCloud):
    def __init__(self) -> None:
        super().__init__()
        self.results: dict[str, dict] = {}
        self.findings: dict[str, dict] = {}
        self.objects: dict[str, Path] = {}
        self.session_results: dict[str, dict] = {}

    def upload_frame(self, object_path, path, content_type, *, replace=False):
        self._maybe_fail()
        self.objects[object_path] = path

    def upsert_result(self, row):
        self._maybe_fail()
        self.results[row["flight_id"]] = row

    def upsert_findings(self, rows):
        self._maybe_fail()
        for row in rows:
            self.findings[row["id"]] = row

    def delete_findings_except(self, flight_id, keep):
        self._maybe_fail()
        self.findings = {k: v for k, v in self.findings.items()
                         if v["flight_id"] != flight_id or k in keep}

    def upsert_session_result(self, row):
        self._maybe_fail()
        self.session_results[row["session_id"]] = row

    def delete_session_findings_except(self, session_id, keep):
        self._maybe_fail()
        self.findings = {k: v for k, v in self.findings.items()
                         if not (v.get("scope") == "session" and v["session_id"] == session_id)
                         or k in keep}


def finding(fid: str, severity: str = "warning") -> dict:
    return {"id": fid, "signal": "temperature", "severity": severity, "title": "Warmer",
            "sentence": "From 0:42…", "start_index": 1, "end_index": 30, "t_start_s": 0.1,
            "t_end_s": 3.0, "unit": "C", "observed": 33.0, "expected": 31.0, "delta": 2.0,
            "z": 9.0, "point_ids": ["P1"], "x_m": None, "y_m": None, "z_m": None}


def write_result(folder: Path, *, findings: list[dict],
                 session: str | None = "session-1") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / results.RESULT_NAME).write_text(json.dumps({
        "flight_id": "flight-1", "session_id": session, "pipeline_version": "2",
        "stages": {"clean": "robust@1"}, "created_at": "2026-10-09T08:00:00+00:00",
        "temp_unit": "C", "points": [{"point_id": "P1", "verdict": "anomaly"}],
        "findings": findings,
        "flags": [{"index": 3, "column": "raw_temp", "kind": "spike", "reason": "a spike"}],
    }))


@pytest.fixture
def rig(tmp_path):
    outbox = Outbox(tmp_path / "outbox")
    cloud = ResultsCloud()
    return outbox, cloud, Syncer(outbox, lambda: cloud, sleep=lambda s: None), tmp_path


def queue(outbox: Outbox, folder: Path) -> None:
    outbox.put(Kind.RESULTS, "flight-1", {"flight_id": "flight-1", "folder": str(folder),
                                          "occurred_at": "2026-10-09T08:00:00+00:00"})


class TestTheRows:
    def test_the_result_row_counts_findings_and_names_the_worst(self, tmp_path):
        write_result(tmp_path, findings=[finding("a", "info"), finding("b", "critical")])
        row = results.result_row(results.load(tmp_path))
        assert row["findings_count"] == 2 and row["worst_severity"] == "critical"
        assert row["flags"] == [[3, "raw_temp", "spike", "a spike"]]

    def test_no_findings_is_no_worst(self, tmp_path):
        write_result(tmp_path, findings=[])
        assert results.result_row(results.load(tmp_path))["worst_severity"] is None

    def test_a_finding_row_carries_its_flight_and_session(self, tmp_path):
        write_result(tmp_path, findings=[finding("a")])
        (row,) = results.finding_rows(results.load(tmp_path))
        assert (row["flight_id"], row["session_id"], row["pipeline_version"]) == \
            ("flight-1", "session-1", "2")

    def test_the_retired_image_columns_are_sent_neutral(self, tmp_path):
        """The hosted table still requires image_support (migration
        20261009000016); results carry no frames now, and an older result that
        did is sent neutral all the same."""
        old = {**finding("a"), "evidence_frames": [2], "image_support": "supports",
               "image_note": "the view changed"}
        write_result(tmp_path, findings=[finding("b"), old])
        for row in results.finding_rows(results.load(tmp_path)):
            assert (row["evidence_frames"], row["image_support"], row["image_note"]) == \
                ([], "cannot_tell", "")

    def test_a_file_that_is_not_a_result_says_so(self, tmp_path):
        (tmp_path / results.RESULT_NAME).write_text("{}")
        with pytest.raises(results.ResultUnreadable):
            results.load(tmp_path)


class TestTheUpload:
    def test_it_waits_for_the_flights_own_row(self, rig):
        outbox, cloud, syncer, tmp = rig
        write_result(tmp / "r", findings=[finding("a")])
        outbox.put(Kind.FLIGHT, "flight-1", {"id": "flight-1", "_sent": False})
        queue(outbox, tmp / "r")
        syncer._send_results(cloud)
        assert cloud.results == {} and outbox.count_pending(Kind.RESULTS) == 1

    def test_the_result_then_its_findings_and_no_frames(self, rig):
        outbox, cloud, syncer, tmp = rig
        write_result(tmp / "r", findings=[finding("a"), finding("b", "info")])
        queue(outbox, tmp / "r")
        syncer.sync_once()
        assert cloud.objects == {}
        assert cloud.results["flight-1"]["findings_count"] == 2
        assert set(cloud.findings) == {"a", "b"}
        assert outbox.count_pending(Kind.RESULTS) == 0

    def test_a_reprocess_replaces_and_retracts(self, rig):
        outbox, cloud, syncer, tmp = rig
        write_result(tmp / "r", findings=[finding("a"), finding("b")])
        queue(outbox, tmp / "r")
        syncer.sync_once()
        write_result(tmp / "r", findings=[finding("a", "critical")])
        queue(outbox, tmp / "r")                       # processed again
        syncer.sync_once()
        assert set(cloud.findings) == {"a"} and cloud.findings["a"]["severity"] == "critical"
        assert cloud.results["flight-1"]["findings_count"] == 1

    def test_offline_everything_waits_and_resumes(self, rig):
        outbox, cloud, syncer, tmp = rig
        write_result(tmp / "r", findings=[finding("a")])
        queue(outbox, tmp / "r")
        cloud.fail_with = CloudError("offline")
        syncer.sync_once()
        assert outbox.count_pending(Kind.RESULTS) == 1 and cloud.results == {}
        cloud.fail_with = None
        syncer.sync_once()
        assert "flight-1" in cloud.results and set(cloud.findings) == {"a"}

    def test_a_flight_with_no_session_still_uploads_its_result(self, rig):
        outbox, cloud, syncer, tmp = rig
        write_result(tmp / "r", findings=[], session=None)
        queue(outbox, tmp / "r")
        syncer.sync_once()
        assert "flight-1" in cloud.results

    def test_a_missing_result_file_is_dropped_not_retried_forever(self, rig):
        outbox, cloud, syncer, tmp = rig
        queue(outbox, tmp / "gone")
        syncer.sync_once()
        assert outbox.count_pending(Kind.RESULTS) == 0 and cloud.results == {}


def test_processing_a_flight_queues_its_result(tmp_path, monkeypatch, capsys):
    root = tmp_path / "data"
    shutil.copytree(FIXTURE, root)
    monkeypatch.setenv("DRONEDECK_DATA_DIR", str(root))
    assert main(["process", "--flight", FLIGHT]) == 0
    assert "queued for upload" in capsys.readouterr().out
    record = Outbox(root / "outbox").get(Kind.RESULTS, FLIGHT)
    assert record and Path(record["folder"]) == root / "results" / FLIGHT


def write_session_result(folder: Path, *, findings: list[dict]) -> None:
    write_result(folder, findings=findings, session="session-1")
    data = json.loads((folder / results.RESULT_NAME).read_text())
    data.update(flight_id="session-1", scope="session",
                points=[{"point_id": "session", "verdict": "normal"}])
    (folder / results.RESULT_NAME).write_text(json.dumps(data))


class TestASessionsOwnResult:
    """A session processed as a whole (migration 20261009000018): its row in
    pipeline_session_results, its findings scope "session" with no flight."""

    def test_the_rows_name_the_session_and_no_flight(self, tmp_path):
        write_session_result(tmp_path, findings=[finding("s1")])
        result = results.load(tmp_path)
        row = results.session_result_row(result)
        assert "flight_id" not in row and row["session_id"] == "session-1"
        (f,) = results.finding_rows(result)
        assert (f["flight_id"], f["session_id"], f["scope"]) == (None, "session-1", "session")

    def test_a_flights_finding_says_flight(self, tmp_path):
        write_result(tmp_path, findings=[finding("a")])
        (f,) = results.finding_rows(results.load(tmp_path))
        assert f["scope"] == "flight" and f["flight_id"] == "flight-1"

    def test_it_waits_for_the_sessions_row_then_uploads(self, rig):
        outbox, cloud, syncer, tmp = rig
        write_session_result(tmp / "s", findings=[finding("s1")])
        outbox.put(Kind.SESSION, "session-1", {"id": "session-1"})
        outbox.put(Kind.SESSION_RESULTS, "session-1", {"session_id": "session-1",
                                                       "folder": str(tmp / "s")})
        syncer._send_session_results(cloud)
        assert cloud.session_results == {}
        outbox.mark_sent(Kind.SESSION, "session-1")
        syncer._send_session_results(cloud)
        assert cloud.session_results["session-1"]["findings_count"] == 1
        assert cloud.findings["s1"]["scope"] == "session"
        assert cloud.objects == {}
        assert outbox.count_pending(Kind.SESSION_RESULTS) == 0

    def test_a_reprocess_retracts_only_the_sessions_own_findings(self, rig):
        outbox, cloud, syncer, tmp = rig
        cloud.findings["flight-f"] = {**finding("flight-f"), "flight_id": "flight-1",
                                      "session_id": "session-1", "scope": "flight"}
        cloud.findings["old"] = {**finding("old"), "flight_id": None,
                                 "session_id": "session-1", "scope": "session"}
        write_session_result(tmp / "s", findings=[finding("s1")])
        outbox.put(Kind.SESSION_RESULTS, "session-1", {"session_id": "session-1",
                                                       "folder": str(tmp / "s")})
        syncer._send_session_results(cloud)
        assert set(cloud.findings) == {"flight-f", "s1"}
