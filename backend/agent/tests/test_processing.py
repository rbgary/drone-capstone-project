"""The DPP switch: flights processed by the data pipeline when they land, one
at a time, in a process of their own — chosen per session, on by default in
Auto and off in Manual."""

from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dronedeck import history
from dronedeck.api import rest
from dronedeck.api.tokens import HEADER
from dronedeck.processing import JobState, ProcessingQueue, default_command
from dronedeck.session import Mode, Session, SessionError, State
from dronedeck.sync.cloud import Operator
from dronedeck.sync.outbox import Kind, Outbox
from tests.test_session import FakeLink, sign_in, start_and_confirm, wait_for
from tests.test_sync import FakeCloud


def succeeds(_flight_id: str) -> list[str]:
    return [sys.executable, "-c", "print('saved')"]


def fails(flight_id: str) -> list[str]:
    return [sys.executable, "-c",
            f"print('  No readings for flight {flight_id} on this computer.'); "
            f"raise SystemExit(2)"]


def hangs(_flight_id: str) -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(30)"]


class TestTheQueue:
    def test_a_flight_runs_and_is_done(self):
        seen: list[str] = []
        q = ProcessingQueue(command=succeeds, on_change=lambda job: seen.append(job.state))
        q.submit("f1")
        assert q.wait_idle()
        assert q.job("f1").state == JobState.DONE and q.job("f1").error is None
        # The worker marks the job done, THEN notifies: wait_idle can return in
        # between, so wait for the notification itself (this raced ~1 run in 3).
        assert wait_for(lambda: seen[-1:] == ["done"])
        assert seen == ["queued", "running", "done"]

    def test_a_failure_says_why_in_the_pipelines_own_words(self):
        q = ProcessingQueue(command=fails)
        q.submit("f1")
        assert q.wait_idle()
        job = q.job("f1")
        assert job.state == JobState.FAILED
        assert job.error == "No readings for flight f1 on this computer."

    def test_a_stuck_pipeline_is_stopped(self):
        q = ProcessingQueue(command=hangs, timeout_s=0.3)
        q.submit("f1")
        assert q.wait_idle(10)
        assert q.job("f1").state == JobState.FAILED
        assert "longer than" in q.job("f1").error

    def test_a_pipeline_that_cannot_start_is_a_failure_not_a_crash(self):
        q = ProcessingQueue(command=lambda _: ["/no/such/program"])
        q.submit("f1")
        assert q.wait_idle()
        assert "could not be started" in q.job("f1").error

    def test_flights_run_one_at_a_time_in_order(self):
        order: list[str] = []
        q = ProcessingQueue(command=succeeds, on_change=lambda job: job.state == "running"
                            and order.append(job.flight_id))
        for fid in ("a", "b", "c"):
            q.submit(fid)
        assert q.wait_idle()
        assert order == ["a", "b", "c"]

    def test_closing_starts_nothing_that_was_still_waiting(self):
        """The agent shutting down (rest.py lifespan) closes the queue while a
        job runs: that one is stopped and NOTHING queued behind it starts — a
        job started then would outlive the agent, and its result would never be
        recorded, so the next start would run it again. Until 2026-11-16 the
        stop was a FIFO entry and every waiting job ran after close()."""
        started: list[str] = []

        def records(flight_id: str) -> list[str]:
            started.append(flight_id)
            return hangs(flight_id)

        q = ProcessingQueue(command=records, timeout_s=30)
        for fid in ("a", "b", "c"):
            q.submit(fid)
        assert wait_for(lambda: q.job("a").state == JobState.RUNNING)
        q.close()
        time.sleep(1.0)                       # long enough for b to have started
        assert started == ["a"]
        assert q.job("a").state == JobState.FAILED          # stopped, not left running
        assert {q.job("b").state, q.job("c").state} == {JobState.QUEUED}

    def test_a_close_while_the_child_is_starting_still_stops_it(self):
        """close() can land after the child process exists but before the queue
        has recorded it — the one moment close() could not see it to kill it.
        The command factory runs inside that window, so closing from it hits
        the window every time."""
        q = ProcessingQueue(command=lambda fid: (q.close(), hangs(fid))[1], timeout_s=30)
        q.submit("a")
        assert wait_for(lambda: q.job("a").state == JobState.FAILED, timeout=5.0)

    def test_queuing_one_already_waiting_changes_nothing(self):
        q = ProcessingQueue(command=hangs, timeout_s=5)
        first = q.submit("f1")
        assert q.submit("f1") is first
        q.close()

    @pytest.mark.parametrize("bad", ["", "../x", "a/b", "x" * 65, "a b"])
    def test_an_id_that_could_leave_the_folder_is_refused(self, bad):
        with pytest.raises(ValueError):
            ProcessingQueue(command=succeeds).submit(bad)

    def test_the_command_is_the_cli_frozen_or_not(self, monkeypatch):
        assert default_command("f1")[-3:] == ["process", "--flight", "f1"]
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert default_command("f1") == [sys.executable, "process", "--flight", "f1"]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
    cloud = FakeCloud()
    cloud.sign_in = lambda email, password: Operator("user-1", email, "Ada", "operator")
    cloud.sign_out = lambda: None
    link = FakeLink()
    events: list[tuple[str, dict]] = []
    outbox = Outbox(tmp_path / "outbox")
    queue = ProcessingQueue(command=succeeds, session_command=succeeds)
    session = Session(cloud=cloud, outbox=outbox, link_factory=lambda: link,
                      publish=lambda kind, payload: events.append((kind, payload)),
                      processing=queue)
    return SimpleNamespace(session=session, link=link, outbox=outbox, events=events,
                           queue=queue)


def fly_manual_and_land(rig) -> str:
    rig.session.arm_manual()
    flight_id = rig.session.snapshot().flight["id"]
    rig.link.manual_controller.state = "landed"
    assert wait_for(lambda: rig.session.snapshot().state is State.READY)
    rig.link.manual_controller.state = "idle"
    return flight_id


def flight_line(rig, flight_id):
    meta = json.loads((rig.session.history.folder / "meta.json").read_text())
    return next(f for f in meta["flights"] if f["id"] == flight_id)


class TestTheSwitch:
    def test_on_by_default_in_manual_and_in_auto(self, rig):
        sign_in(rig)
        assert rig.session.snapshot().processing["on"] is True
        rig.session.set_mode(Mode.AUTO)
        assert rig.session.snapshot().processing == {
            "on": True, "chosen": False, "jobs": [], "last_flight_id": None}

    def test_a_manual_flight_lands_and_is_processed_by_default(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        flight_id = fly_manual_and_land(rig)
        assert rig.queue.wait_idle()
        assert rig.queue.job(flight_id).state == JobState.DONE

    def test_turned_off_before_the_session_starts_it_holds_for_that_session(self, rig):
        sign_in(rig)
        rig.session.set_processing(False)
        start_and_confirm(rig, Mode.MANUAL)
        flight_id = fly_manual_and_land(rig)
        assert rig.queue.job(flight_id) is None

    def test_a_manual_flight_with_it_off_is_not_processed(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.set_processing(False)
        flight_id = fly_manual_and_land(rig)
        assert rig.queue.job(flight_id) is None
        assert flight_line(rig, flight_id)["processing"] is None
        assert rig.session.snapshot().processing["last_flight_id"] == flight_id

    def test_turned_on_a_manual_flight_is_processed_when_it_lands(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.set_processing(True)
        flight_id = fly_manual_and_land(rig)
        assert rig.queue.wait_idle()
        assert rig.queue.job(flight_id).state == JobState.DONE
        assert wait_for(lambda: flight_line(rig, flight_id)["processing"] == "done")
        jobs = rig.session.snapshot().processing["jobs"]
        assert jobs[0]["flight_id"] == flight_id and jobs[0]["state"] == "done"
        assert any(kind == "processing" for kind, _ in rig.events)

    def test_the_choice_is_read_as_a_flight_begins(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.set_processing(False)
        rig.session.arm_manual()
        flight_id = rig.session.snapshot().flight["id"]
        assert flight_line(rig, flight_id)["processing"] is None
        rig.session.set_processing(True)                 # mid-flight: for the next one
        rig.link.manual_controller.state = "landed"
        assert wait_for(lambda: rig.session.snapshot().state is State.READY)
        assert rig.queue.job(flight_id) is None

    def test_a_choice_made_before_a_session_survives_a_mode_change(self, rig):
        sign_in(rig)
        rig.session.set_processing(False)
        rig.session.set_mode(Mode.AUTO)
        assert rig.session.snapshot().processing["on"] is False

    def test_the_choice_is_per_session(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.set_processing(False)
        rig.session.end()
        assert rig.session.snapshot().processing["on"] is True
        assert rig.session.snapshot().processing["chosen"] is False

    def test_it_is_in_the_audit_trail(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.set_processing(True)
        actions = [r.payload for r in rig.outbox.pending(Kind.AUDIT)]
        assert any(a["action"] == "processing_set" and a["detail"] == {"on": True,
                                                                       "mode": "manual"}
                   for a in actions)

    def test_signed_out_it_cannot_be_set(self, rig):
        with pytest.raises(SessionError, match="Sign in"):
            rig.session.set_processing(True)

    def test_a_flight_can_be_processed_by_hand_afterwards(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.set_processing(False)
        flight_id = fly_manual_and_land(rig)
        rig.session.process_flight(flight_id)
        assert rig.queue.wait_idle()
        assert wait_for(lambda: flight_line(rig, flight_id)["processing"] == "done")

    def test_a_flight_still_recording_cannot_be_processed(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.arm_manual()
        with pytest.raises(SessionError, match="still recording"):
            rig.session.process_flight(rig.session.snapshot().flight["id"])


def test_two_threads_recording_at_once_never_corrupt_the_file(tmp_path):
    """A job reports from the caller's thread and the worker's at once."""
    import threading as _threading
    folder = tmp_path / "s1"
    folder.mkdir()
    (folder / "meta.json").write_text(json.dumps(
        {"flights": [{"id": f"f{i}"} for i in range(8)]}))
    errors: list[BaseException] = []

    def hammer(i: int) -> None:
        try:
            for n in range(40):
                history.set_flight_processing(f"f{i}", f"state-{n}", root=tmp_path)
        except BaseException as e:            # noqa: BLE001 — the test reports it
            errors.append(e)

    threads = [_threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    flights = json.loads((folder / "meta.json").read_text())["flights"]
    assert [f["processing"] for f in flights] == ["state-39"] * 8
    assert list(folder.glob("*.tmp")) == []


def test_a_closed_sessions_flight_is_recorded_in_its_file(tmp_path):
    folder = tmp_path / "s1"
    folder.mkdir()
    (folder / "meta.json").write_text(json.dumps({"flights": [{"id": "f1"}, {"id": "f2"}]}))
    assert history.set_flight_processing("f2", "failed", "why", root=tmp_path)
    flights = json.loads((folder / "meta.json").read_text())["flights"]
    assert flights[1]["processing"] == "failed" and flights[1]["processing_error"] == "why"
    assert "processing" not in flights[0]
    assert not history.set_flight_processing("nope", "done", root=tmp_path)


class TestTheRoutes:
    @pytest.fixture
    def api(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("DRONEDECK_STANDBY", "0")
        cloud = FakeCloud()
        cloud.sign_in = lambda email, password: Operator("user-1", email, "Ada", "operator")
        session = Session(cloud=cloud, outbox=Outbox(tmp_path / "outbox"),
                          link_factory=FakeLink, publish=rest.agent.hub.publish,
                          processing=ProcessingQueue(command=succeeds))
        monkeypatch.setattr(rest.agent, "session", session)
        return SimpleNamespace(client=TestClient(rest.app), root=tmp_path, session=session)

    def headers(self):
        return {HEADER: rest.agent.token}

    @pytest.mark.parametrize("method, path", [
        ("post", "/session/processing"), ("get", "/flights/f1/result"),
        ("post", "/flights/f1/process"),
    ])
    def test_every_route_needs_the_token(self, api, method, path):
        kwargs = {"json": {"on": True}} if method == "post" else {}
        assert getattr(api.client, method)(path, **kwargs).status_code == 401

    def test_the_switch(self, api):
        api.session.sign_in("ada@example.com", "pw")
        body = api.client.post("/session/processing", json={"on": True},
                               headers=self.headers()).json()
        assert body["processing"]["on"] is True

    def test_no_result_yet_is_a_404_in_words(self, api):
        response = api.client.get("/flights/f1/result", headers=self.headers())
        assert response.status_code == 404
        assert "has not been processed" in response.json()["detail"]

    def test_a_result_comes_back_whole(self, api):
        folder = api.root / "results" / "f1"
        folder.mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps({"flight_id": "f1", "points": []}))
        body = api.client.get("/flights/f1/result", headers=self.headers()).json()
        assert body["result"] == {"flight_id": "f1", "points": []} and body["job"] is None

    def test_an_id_that_could_leave_the_folder_is_a_404(self, api):
        response = api.client.get("/flights/..%2Fx/result", headers=self.headers())
        assert response.status_code == 404


class TestResumingAfterTheAppClosed:
    """The app closed while flights waited or were being processed: the next
    start processes them, in the background. A failed one waits to be asked."""

    def write_session(self, root, name, flights):
        folder = root / "sessions" / name
        folder.mkdir(parents=True)
        (folder / "meta.json").write_text(json.dumps({"id": name, "flights": flights}))

    def test_unfinished_flights_are_found_oldest_first(self, tmp_path):
        self.write_session(tmp_path, "s1", [
            {"id": "f-running", "started_at": "2026-10-09T08:03:00Z", "processing": "running"},
            {"id": "f-done", "started_at": "2026-10-09T08:00:00Z", "processing": "done"},
            {"id": "f-off", "started_at": "2026-10-09T08:01:00Z", "processing": None}])
        self.write_session(tmp_path, "s2", [
            {"id": "f-pending", "started_at": "2026-10-09T08:02:00Z", "processing": "pending"},
            {"id": "f-queued", "started_at": "2026-10-09T08:04:00Z", "processing": "queued"},
            {"id": "f-failed", "started_at": "2026-10-09T08:05:00Z", "processing": "failed"}])
        assert history.interrupted_flights(root=tmp_path / "sessions") == [
            "f-pending", "f-running", "f-queued"]

    def test_an_unreadable_session_file_is_skipped(self, tmp_path):
        folder = tmp_path / "sessions" / "bad"
        folder.mkdir(parents=True)
        (folder / "meta.json").write_text("{not json")
        assert history.interrupted_flights(root=tmp_path / "sessions") == []

    def test_the_session_queues_them_and_records_the_outcome(self, rig, tmp_path):
        self.write_session(tmp_path, "old", [
            {"id": "f1", "started_at": "2026-10-09T08:00:00Z", "processing": "running"},
            {"id": "f2", "started_at": "2026-10-09T08:01:00Z", "processing": "failed"}])
        assert rig.session.resume_processing() == ["f1"]
        assert wait_for(lambda: (job := rig.queue.job("f1")) is not None
                        and job.state == JobState.DONE)
        flights = json.loads((tmp_path / "sessions" / "old" / "meta.json").read_text())["flights"]
        assert wait_for(lambda: json.loads((tmp_path / "sessions" / "old" / "meta.json")
                                           .read_text())["flights"][0]["processing"] == "done")
        assert flights[1]["processing"] == "failed"         # left for the operator

    def test_nothing_unfinished_queues_nothing(self, rig):
        assert rig.session.resume_processing() == []


@pytest.mark.parametrize(("setting", "resumed"), [("1", True), ("0", False)])
def test_the_agent_resumes_as_it_starts_unless_told_not_to(monkeypatch, tmp_path, setting,
                                                           resumed):
    monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DRONEDECK_STANDBY", "0")
    monkeypatch.setenv("DRONEDECK_RESUME", setting)
    called, closed = [], []
    monkeypatch.setattr(rest.agent.session, "resume_processing", lambda: called.append(1))
    # rest.agent's outbox is the LAPTOP'S OWN — built when the module was
    # imported, before this test's data folder existed. Unstubbed, this test
    # closed the real laptop's open flight records (found 2026-10-09).
    monkeypatch.setattr(rest, "close_orphaned_flights", lambda outbox: closed.append(outbox))
    with TestClient(rest.app):
        if resumed:
            assert wait_for(lambda: bool(called))
    assert bool(called) is resumed
    assert bool(closed) is resumed


class TestProcessAll:
    """`dronedeck process --all`: the flights recorded before processing
    existed, or by an older pipeline, get a current result and go to the web."""

    @pytest.fixture
    def data(self, tmp_path, monkeypatch):
        import shutil
        from pathlib import Path

        fixture = Path(__file__).parent / "pipeline" / "fixtures" / "data"
        root = tmp_path / "data"
        shutil.copytree(fixture, root)
        empty = root / "sessions" / "empty-session"
        empty.mkdir()
        (empty / "meta.json").write_text(json.dumps({"id": "empty-session", "flights": [
            {"id": "00000000-0000-4000-8000-000000000000", "mode": "manual",
             "started_at": "2026-09-25T08:00:00+00:00"}]}))
        monkeypatch.setenv("DRONEDECK_DATA_DIR", str(root))
        return root

    def test_every_flight_is_processed_and_queued_and_an_empty_one_skipped(self, data, capsys):
        from dronedeck import cli, paths

        assert cli.main(["process", "--all"]) == 0
        out = capsys.readouterr().out
        assert "processed 1" in out and "1 with no readings" in out
        flight = "372bbdc4-d323-42bb-9e6c-29ef02e3794c"
        assert (paths.results_dir() / flight / "result.json").exists()
        assert Outbox().get(Kind.RESULTS, flight) is not None
        meta = json.loads((data / "sessions" / "a5788fe1-02fb-49d7-ac1c-b400d825d45f"
                           / "meta.json").read_text())
        assert meta["flights"][0]["processing"] == "done"

    def test_running_it_again_only_does_what_is_left(self, data, capsys):
        from dronedeck import cli

        cli.main(["process", "--all"])
        capsys.readouterr()
        assert cli.main(["process", "--all"]) == 0
        assert "processed 0" in capsys.readouterr().out


class TestTheSessionItself:
    """The session is processed too — its samples around the flights — when it
    ends with the DPP switch on, in Manual as in Auto (the owner, 2026-10-09)."""

    def test_a_session_job_runs_the_session_command(self):
        ran: list[str] = []

        def session_command(session_id: str) -> list[str]:
            ran.append(session_id)
            return succeeds(session_id)

        queue = ProcessingQueue(command=fails, session_command=session_command)
        job = queue.submit("s-1", kind="session")
        assert job.kind == "session"
        assert queue.wait_idle()
        assert queue.job("s-1").state == JobState.DONE and ran == ["s-1"]

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError, match="kind"):
            ProcessingQueue(command=succeeds).submit("s-1", kind="mission")

    def test_ending_a_session_with_dpp_on_processes_it(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.set_processing(True)
        session_id = rig.session.snapshot().session_id
        folder = rig.session.history.folder
        rig.session.end()
        assert rig.queue.wait_idle()
        job = rig.queue.job(session_id)
        assert job is not None and job.kind == "session" and job.state == JobState.DONE
        assert wait_for(lambda: json.loads((folder / "meta.json").read_text())
                        .get("processing") == "done")

    def test_ending_a_session_with_dpp_off_does_not(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        session_id = rig.session.snapshot().session_id
        rig.session.set_processing(False)
        rig.session.end()
        assert rig.queue.wait_idle()
        assert rig.queue.job(session_id) is None

    def test_unfinished_sessions_are_found_and_resumed(self, rig, tmp_path):
        for name, state, ended in (("s-old", "running", True), ("s-done", "done", True),
                                   ("s-open", "queued", False), ("s-failed", "failed", True)):
            folder = tmp_path / "sessions" / name
            folder.mkdir(parents=True)
            (folder / "meta.json").write_text(json.dumps({
                "id": name, "started_at": "2026-10-09T08:00:00Z", "flights": [],
                "ended_at": "2026-10-09T08:10:00Z" if ended else None, "processing": state}))
        assert history.interrupted_sessions(root=tmp_path / "sessions") == ["s-old"]
        assert "s-old" in rig.session.resume_processing()
        assert rig.queue.wait_idle()
        assert rig.queue.job("s-old").kind == "session"


class TestNothingIsJudgedInFlight:
    """A flight is processed once its data has arrived (2026-11-15): a point's
    hold ending queues nothing, even with the DPP on."""

    def test_a_completed_point_queues_nothing(self, rig):
        from dronedeck.mission.controller import EventKind, MissionEvent

        rig.session._flight_id, rig.session._flight_process = "f-air", True
        rig.session._on_mission_event(MissionEvent(EventKind.POINT_COMPLETE, 1.0, "P1",
                                                   "P1 complete"))
        assert rig.queue.jobs() == []
        assert "live" not in rig.session.snapshot().processing

    def test_there_is_no_live_kind_of_job(self, rig):
        with pytest.raises(ValueError):
            rig.queue.submit("f-air", kind="live")
