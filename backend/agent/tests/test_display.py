"""The display chain: a session's captured frames upscaled after the pipeline,
in chunks, never in flight (dronedeck/display/)."""

from __future__ import annotations

import csv
import json
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from dronedeck import cli
from dronedeck.camera.recording import COLUMNS as FRAME_COLUMNS
from dronedeck.display import frames
from dronedeck.display.queue import DisplayJob, DisplayQueue
from dronedeck.display.steps import MODELS_DIR, ModelUnavailable, Upscale, steps_key
from dronedeck.processing import SESSION as SESSION_KIND
from dronedeck.processing import Job, JobState, ProcessingQueue
from dronedeck.session import Mode, Session
from dronedeck.sync.cloud import Operator
from dronedeck.sync.outbox import Outbox
from tests.test_processing import fly_manual_and_land, succeeds
from tests.test_session import FakeLink, start_and_confirm, wait_for
from tests.test_sync import FakeCloud

SESSION = "s-1"
FLIGHT = "f-1"


class Times4:
    """A stand-in step: nearest-neighbour ×4, instantly."""

    name = "times4"
    version = "1"

    def run(self, gray):
        return cv2.resize(gray, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)


class Breaks:
    name = "breaks"
    version = "1"

    def run(self, gray):
        raise RuntimeError("no")


def make_session(root: Path, rows: list[dict], *, evidence: list[int] = (),
                 session_evidence: list[int] = (), session_id: str = SESSION,
                 flight_id: str = FLIGHT) -> tuple[Path, Path]:
    """A session folder with frames.csv and its frames, one flight, and the
    results its findings point into. Returns (session folder, results root)."""
    folder = root / "sessions" / session_id
    (folder / "frames").mkdir(parents=True)
    with (folder / "frames.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FRAME_COLUMNS)
        writer.writeheader()
        for row in rows:
            seq = row["seq"]
            name = f"frames/{seq:06d}.png"
            if row.get("file", True):
                cv2.imwrite(str(folder / name), np.full((6, 8), seq, np.uint8))
            writer.writerow({c: "" for c in FRAME_COLUMNS} | {
                "seq": seq, "t_s": seq / 10, "file": name,
                "upload": "1" if row.get("upload", True) else "0",
                "source": row.get("source", "auto")})
    (folder / "meta.json").write_text(
        json.dumps({"id": session_id, "flights": [{"id": flight_id}]}))
    results = root / "results"
    for where, seqs in ((results / flight_id, evidence),
                        (results / "sessions" / session_id, session_evidence)):
        where.mkdir(parents=True)
        (where / "result.json").write_text(
            json.dumps({"findings": [{"evidence_frames": list(seqs)}]}))
    return folder, results


def index_rows(folder: Path) -> list[dict]:
    with (folder / frames.INDEX).open(newline="") as f:
        return list(csv.DictReader(f))


class TestWhichFrames:
    def test_captured_only_findings_first_then_manual_then_the_rest(self, tmp_path):
        folder, results = make_session(tmp_path, [
            {"seq": 1}, {"seq": 2, "upload": False}, {"seq": 3, "source": "manual"},
            {"seq": 4}, {"seq": 5}, {"seq": 6}], evidence=[5], session_evidence=[4])
        assert [seq for seq, _ in frames.wanted(folder, results)] == [4, 5, 3, 1, 6]

    def test_a_session_with_no_results_is_in_frame_order(self, tmp_path):
        folder, results = make_session(tmp_path, [{"seq": 2}, {"seq": 1}])
        shutil.rmtree(results)
        assert [seq for seq, _ in frames.wanted(folder, results)] == [1, 2]


class TestChunks:
    def test_a_chunk_does_its_share_and_says_what_is_left(self, tmp_path):
        folder, results = make_session(tmp_path, [{"seq": n} for n in range(1, 6)])
        chunk = frames.run_chunk(folder, results, [Times4()], limit=2)
        assert chunk.to_dict() == {"done": 2, "failed": 0, "remaining": 3}
        out = cv2.imread(str(folder / "upscaled" / "000001.webp"), cv2.IMREAD_GRAYSCALE)
        assert out.shape == (24, 32)
        assert [r["seq"] for r in index_rows(folder)] == ["1", "2"]

    def test_the_next_chunk_carries_on_and_the_last_finishes(self, tmp_path):
        folder, results = make_session(tmp_path, [{"seq": n} for n in range(1, 6)])
        frames.run_chunk(folder, results, [Times4()], limit=2)
        frames.run_chunk(folder, results, [Times4()], limit=2)
        assert frames.run_chunk(folder, results, [Times4()], limit=2).remaining == 0
        assert sorted(int(r["seq"]) for r in index_rows(folder)) == [1, 2, 3, 4, 5]
        assert frames.run_chunk(folder, results, [Times4()]).to_dict() == {
            "done": 0, "failed": 0, "remaining": 0}

    def test_new_steps_make_every_frame_again(self, tmp_path):
        folder, results = make_session(tmp_path, [{"seq": 1}, {"seq": 2}])
        frames.run_chunk(folder, results, [Times4()])
        newer = Times4()
        newer.version = "2"
        assert frames.run_chunk(folder, results, [newer]).done == 2
        assert {r["steps"] for r in index_rows(folder)} == {"times4@1", "times4@2"}

    def test_a_broken_frame_is_recorded_and_the_rest_are_made(self, tmp_path):
        folder, results = make_session(tmp_path, [{"seq": 1}, {"seq": 2, "file": False},
                                                  {"seq": 3}])
        chunk = frames.run_chunk(folder, results, [Times4()])
        assert chunk.to_dict() == {"done": 2, "failed": 1, "remaining": 0}
        failed = next(r for r in index_rows(folder) if r["seq"] == "2")
        assert failed["error"] and not failed["file"]
        # not tried again at the same version, so a corrupt file cannot hold
        # the session up for ever
        assert frames.run_chunk(folder, results, [Times4()]).remaining == 0

    def test_a_step_that_fails_leaves_no_file_behind(self, tmp_path):
        folder, results = make_session(tmp_path, [{"seq": 1}])
        assert frames.run_chunk(folder, results, [Breaks()]).failed == 1
        assert list((folder / "upscaled").iterdir()) == []


def append_frame(folder: Path, seq: int) -> None:
    """As the recorder does it (camera/recording.py _write): the file, then its row."""
    name = f"frames/{seq:06d}.png"
    cv2.imwrite(str(folder / name), np.full((6, 8), seq, np.uint8))
    with (folder / "frames.csv").open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=FRAME_COLUMNS).writerow(
            {c: "" for c in FRAME_COLUMNS} | {"seq": seq, "t_s": seq / 10, "file": name,
                                              "upload": "1", "source": "auto"})


class TestTheFile:
    def test_an_upscaled_frame_is_saved_as_webp(self, tmp_path):
        folder, results = make_session(tmp_path, [{"seq": 1}])
        frames.run_chunk(folder, results, [Times4()])
        data = (folder / "upscaled" / "000001.webp").read_bytes()
        assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        assert index_rows(folder)[0]["file"] == "upscaled/000001.webp"


class TestWhileStillRecording:
    def test_a_frame_recorded_between_chunks_is_done_by_the_next(self, tmp_path):
        folder, results = make_session(tmp_path, [{"seq": 1}, {"seq": 2}])
        assert frames.run_chunk(folder, results, [Times4()]).remaining == 0
        append_frame(folder, 3)
        chunk = frames.run_chunk(folder, results, [Times4()])
        assert chunk.to_dict() == {"done": 1, "failed": 0, "remaining": 0}
        assert (folder / "upscaled" / "000003.webp").exists()

    def test_a_row_caught_half_written_is_left_for_later(self, tmp_path):
        folder, results = make_session(tmp_path, [{"seq": 1}])
        with (folder / "frames.csv").open("a", newline="") as f:
            f.write("2,2026-11-04T17:")  # the recorder mid-append
        chunk = frames.run_chunk(folder, results, [Times4()])
        assert chunk.to_dict() == {"done": 1, "failed": 0, "remaining": 0}
        assert [r["seq"] for r in index_rows(folder)] == ["1"]


class TestState:
    def test_what_was_left_queued_or_running_is_resumed_and_failed_is_not(self, tmp_path):
        for session_id, state in (("a", "running"), ("b", "done"), ("c", "failed"),
                                  ("d", "queued")):
            (tmp_path / session_id).mkdir()
            frames.set_state(tmp_path / session_id, state)
        assert sorted(frames.interrupted(tmp_path)) == ["a", "d"]


def counting(tmp_path: Path, total: int, per_chunk: int):
    """A stand-in for `dronedeck upscale --chunk`: each run takes `per_chunk`
    off a counter on disk and says what is left, as the real one does."""
    counter = tmp_path / "left"
    counter.write_text(str(total))
    script = (
        "import json, pathlib, sys\n"
        "p = pathlib.Path(sys.argv[1]); left = int(p.read_text())\n"
        f"n = min({per_chunk}, left); p.write_text(str(left - n))\n"
        "print(json.dumps({'done': n, 'failed': 0, 'remaining': left - n}))\n")
    runs: list[str] = []

    def command(session_id: str, chunk: int) -> list[str]:
        runs.append(session_id)
        return [sys.executable, "-c", script, str(counter)]

    return command, runs


def says(text: str, code: int = 0):
    return lambda _sid, _chunk: [sys.executable, "-c",
                                 f"print({text!r}); raise SystemExit({code})"]


class TestTheQueue:
    def test_chunk_after_chunk_until_nothing_is_left(self, tmp_path):
        command, runs = counting(tmp_path, total=7, per_chunk=3)
        q = DisplayQueue(command=command)
        job = q.submit("session-1")
        assert q.wait_idle()
        assert (job.state, job.made, job.remaining) == ("done", 7, 0)
        assert len(runs) == 3

    def test_nothing_runs_while_a_flight_is_in_the_air(self, tmp_path):
        command, runs = counting(tmp_path, total=2, per_chunk=2)
        flying = {"now": True}
        q = DisplayQueue(command=command, poll_s=0.01)
        q.paused = lambda: flying["now"]
        job = q.submit("session-1")
        time.sleep(0.2)
        assert runs == [] and job.state == "running"
        flying["now"] = False
        assert q.wait_idle()
        assert job.state == "done"

    def test_a_failure_says_why_in_the_commands_own_words(self):
        q = DisplayQueue(command=says("  the display chain cannot run: no model", 3))
        job = q.submit("session-1")
        assert q.wait_idle()
        assert job.state == "failed"
        assert "no model" in job.error

    def test_a_chunk_that_makes_nothing_stops_rather_than_loops(self):
        q = DisplayQueue(command=says('{"done": 0, "failed": 0, "remaining": 4}'))
        job = q.submit("session-1")
        assert q.wait_idle()
        assert job.state == "failed" and "loop" in job.error

    def test_a_stuck_chunk_is_stopped(self):
        q = DisplayQueue(command=lambda _s, _c: [sys.executable, "-c",
                                                 "import time; time.sleep(30)"],
                         timeout_s=0.3)
        job = q.submit("session-1")
        assert q.wait_idle()
        assert job.state == "failed" and "was stopped" in job.error

    def test_a_report_it_cannot_read_is_a_failure(self):
        q = DisplayQueue(command=says("something else"))
        job = q.submit("session-1")
        assert q.wait_idle()
        assert job.state == "failed"

    def test_queuing_one_already_waiting_changes_nothing(self, tmp_path):
        command, _ = counting(tmp_path, total=1, per_chunk=1)
        q = DisplayQueue(command=command)
        q.paused = lambda: True
        first = q.submit("session-1")
        assert q.submit("session-1") is first
        q.close()

    @pytest.mark.parametrize("bad", ["../x", "a/b", "", "a b"])
    def test_an_id_that_could_leave_the_folder_is_refused(self, bad):
        with pytest.raises(ValueError):
            DisplayQueue().submit(bad)

    def test_closed_while_running_it_stays_running_for_the_resume(self, tmp_path):
        command, _ = counting(tmp_path, total=1, per_chunk=1)
        q = DisplayQueue(command=command, poll_s=0.01)
        q.paused = lambda: True
        job = q.submit("session-1")
        assert wait_for(lambda: job.state == "running")
        q.close()
        time.sleep(0.05)
        assert job.state == "running"


class FakeDisplay:
    def __init__(self) -> None:
        self.paused = lambda: False
        self.on_change = None
        self.submitted: list[str] = []

    def submit(self, session_id: str):
        self.submitted.append(session_id)
        return DisplayJob(session_id)

    def close(self) -> None: ...


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
    cloud = FakeCloud()
    cloud.sign_in = lambda email, password: Operator("user-1", email, "Ada", "operator")
    cloud.sign_out = lambda: None
    link = FakeLink()
    queue = ProcessingQueue(command=succeeds, session_command=succeeds)
    display = FakeDisplay()
    session = Session(cloud=cloud, outbox=Outbox(tmp_path / "outbox"),
                      link_factory=lambda: link, publish=lambda kind, payload: None,
                      processing=queue, display=display)
    return SimpleNamespace(session=session, link=link, queue=queue, display=display,
                           root=tmp_path)


class TestTheSession:
    def test_a_processed_flight_queues_its_session(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        flight_id = fly_manual_and_land(rig)
        assert rig.queue.wait_idle()
        assert rig.queue.job(flight_id).state == JobState.DONE
        assert wait_for(lambda: rig.display.submitted == [rig.session.history.meta.id])

    def test_a_flight_not_processed_queues_nothing(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.set_processing(False)
        fly_manual_and_land(rig)
        time.sleep(0.1)
        assert rig.display.submitted == []

    def test_the_sessions_own_processing_queues_it(self, rig):
        rig.session._processing_changed(Job("s-2", state=JobState.DONE, kind=SESSION_KIND))
        assert rig.display.submitted == ["s-2"]

    def test_a_flight_of_a_closed_session_queues_that_session(self, rig):
        folder = rig.root / "sessions" / "s-closed"
        folder.mkdir(parents=True)
        (folder / "meta.json").write_text(json.dumps(
            {"id": "s-closed", "ended_at": "2026-11-04T10:00:00+00:00",
             "flights": [{"id": "f-old"}]}))
        rig.session._processing_changed(Job("f-old", state=JobState.DONE))
        assert rig.display.submitted == ["s-closed"]

    def test_a_flight_no_session_here_recorded_queues_nothing(self, rig):
        rig.session._processing_changed(Job("f-nowhere", state=JobState.DONE))
        assert rig.display.submitted == []

    def test_a_failed_processing_queues_nothing(self, rig):
        rig.session._processing_changed(Job("s-3", state=JobState.FAILED, kind=SESSION_KIND))
        assert rig.display.submitted == []

    def test_paused_while_a_flight_is_in_the_air(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        assert rig.display.paused() is False
        rig.session.arm_manual()
        assert rig.display.paused() is True

    def test_its_state_is_kept_beside_the_session_and_resumed(self, rig):
        folder = rig.root / "sessions" / "s-9"
        folder.mkdir(parents=True)
        rig.display.on_change(DisplayJob("s-9", state="running"))
        assert json.loads((folder / frames.STATE).read_text())["state"] == "running"
        assert rig.session.resume_display() == ["s-9"]
        assert rig.display.submitted == ["s-9"]


class TestTheModel:
    def test_real_esrgan_then_clahe_makes_a_frame_four_times_as_wide(self):
        frame = (np.indices((30, 40)).sum(axis=0) * 3 % 256).astype(np.uint8)
        out = Upscale().run(frame)
        assert out.shape == (120, 160) and out.dtype == np.uint8

    def test_the_step_names_what_made_a_frame(self):
        assert steps_key([Upscale()]) == "realesrgan-x4+clahe-1.5-tiles4@1"

    def test_a_missing_model_says_so(self, tmp_path):
        (tmp_path / "MODELS.json").write_text((MODELS_DIR / "MODELS.json").read_text())
        with pytest.raises(ModelUnavailable, match="not installed"):
            Upscale(models_dir=tmp_path)

    def test_an_altered_model_is_never_run(self, tmp_path):
        (tmp_path / "MODELS.json").write_text((MODELS_DIR / "MODELS.json").read_text())
        (tmp_path / "realesr-general-x4v3.onnx").write_bytes(b"not the model")
        with pytest.raises(ModelUnavailable, match="not the file"):
            Upscale(models_dir=tmp_path)


class TestTheCommand:
    def test_a_chunk_reports_on_its_last_line(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
        folder, _ = make_session(tmp_path, [{"seq": n} for n in range(1, 4)])
        assert cli.main(["upscale", "--session", SESSION, "--chunk", "2"]) == 0
        last = capsys.readouterr().out.strip().splitlines()[-1]
        assert json.loads(last) == {"done": 2, "failed": 0, "remaining": 1}
        assert (folder / "upscaled" / "000001.webp").exists()

    def test_a_session_with_no_frames_here_is_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
        assert cli.main(["upscale", "--session", "nothing-here"]) == 2

    def test_all_does_every_session_and_only_what_is_left(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
        one, _ = make_session(tmp_path, [{"seq": 1}, {"seq": 2}], session_id="s-a",
                              flight_id="f-a")
        two, _ = make_session(tmp_path, [{"seq": 1}], session_id="s-b", flight_id="f-b")
        assert cli.main(["upscale", "--all"]) == 0
        assert "3 frame(s) made" in capsys.readouterr().out
        assert len(list((one / "upscaled").iterdir())) == 2
        assert len(list((two / "upscaled").iterdir())) == 1
        assert cli.main(["upscale", "--all"]) == 0
        assert "0 frame(s) made" in capsys.readouterr().out

    def test_selftest_fails_a_bundle_without_the_model(self, monkeypatch, capsys):
        from dronedeck.display import steps

        def missing():
            raise ModelUnavailable("realesr-general-x4v3.onnx is not installed")

        monkeypatch.setattr(steps, "default_steps", missing)
        assert cli.main(["selftest"]) == 1
        assert "FAILED  display model" in capsys.readouterr().out

    def test_selftest_passes_with_the_model(self, capsys):
        assert cli.main(["selftest"]) == 0


class TestEndToEnd:
    def test_the_real_queue_runs_the_real_command_on_real_child_processes(
            self, tmp_path, monkeypatch):
        """No stand-ins: DisplayQueue's own command, `dronedeck upscale`, the
        model — two chunks of a three-frame session."""
        monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))  # the children inherit it
        folder, _ = make_session(tmp_path, [{"seq": n} for n in (1, 2, 3)],
                                 session_id="s-e2e", flight_id="f-e2e")
        progress: list[tuple[str, int, int | None]] = []
        q = DisplayQueue(chunk=2, on_change=lambda j: progress.append(
            (j.state, j.made, j.remaining)))
        job = q.submit("s-e2e")
        assert q.wait_idle(timeout_s=120)
        assert (job.state, job.made, job.failed, job.error) == ("done", 3, 0, None)
        assert ("running", 2, 1) in progress
        for seq in (1, 2, 3):
            out = cv2.imread(str(folder / "upscaled" / f"{seq:06d}.webp"), cv2.IMREAD_GRAYSCALE)
            assert out.shape == (24, 32)
