# This file was modified on 11/20/2026 with the assistance of Claude Code in VS Code
# (Claude Opus 5.5, Anthropic, 2026). Pins that a completed hover test records no abort
# reason, and that a hover test refuses a drone whose motors are locked.

"""The operating session: nothing flies without an operator, checks and a
confirmed area, and every flight ends recorded."""

from __future__ import annotations

import threading
import time
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from dronedeck.flight.checks import CheckKey, CheckResult, ChecksFailed, CheckStatus, ReadyReport
from dronedeck.flight.link import LinkError
from dronedeck.flight.programs import Outcome, ProgramResult
from dronedeck.safety.flight_guard import Action as GuardAction
from dronedeck.safety.flight_guard import PositioningStatus, Reason, Verdict
from dronedeck.session import Mode, Session, SessionError, State
from dronedeck.sync.cloud import AuthError, Operator
from dronedeck.sync.outbox import Kind, Outbox
from dronedeck.telemetry.stream import Snapshot
from tests.test_sync import FakeCloud

REPORT = ReadyReport(
    hardware_id="cf-lab", vbat=4.05, endurance_s=200.0, ground_z_m=0.012,
    takeoff_xy=(0.10, -0.20), estimate_spread_m=0.005,
    positioning=PositioningStatus((0, 1), (0, 1), (0, 1), (0, 1), (0.0004,) * 3),
)


class FakeStream:
    def __init__(self) -> None:
        self.subscribers: list = []

    def snapshot(self) -> Snapshot:
        return Snapshot(MappingProxyType({"baro.temp": 30.0, "baro.pressure": 1013.0}), 1.0)

    def subscribe(self, cb):
        self.subscribers.append(cb)
        return lambda: self.subscribers.remove(cb)


class FakeFlight:
    def __init__(self) -> None:
        self.landed = self.stopped = False

    def request_land(self) -> None:
        self.landed = True

    def request_stop(self) -> None:
        self.stopped = True


class FakeManual:
    def __init__(self) -> None:
        self.state = "armed"
        self.ground_z = 0.012
        self.events: list[str] = []

    def arm(self): self.events.append("arm")
    def start(self): self.events.append("start")
    def stop(self): self.events.append("stop")
    def land(self): self.events.append("land")
    def emergency_stop(self): self.events.append("emergency_stop")
    def set_intent(self, intent): self.events.append("intent")
    def heartbeat(self): self.events.append("heartbeat")

    def set_key_frame(self, frame, operator_xy):
        self.key_frame = (frame, operator_xy)

    def set_frame_listener(self, listener):
        self.frame_listener = listener


class FakeLink:
    uri = "radio://0/80/2M"

    def __init__(self) -> None:
        self.is_open = False
        self.stream = FakeStream()
        self.report = REPORT
        self.open_error: Exception | None = None
        self.check_failure: CheckResult | None = None
        self.health_result: Any = SimpleNamespace(
            ok=True, motors=SimpleNamespace(failed=()),
            battery=SimpleNamespace(passed=True, sag_v=0.2),
            battery_error=None, to_dict=lambda: {"ok": True})
        self.health_error: Exception | None = None
        self.checks_runs = 0
        self.flight = FakeFlight()
        self.manual_controller = FakeManual()
        self.closed = False
        self.guard_verdict: Verdict | None = None

    def open(self) -> None:
        if self.open_error:
            raise self.open_error
        self.is_open = True

    def close(self) -> None:
        self.is_open = False
        self.closed = True

    def snapshot(self) -> Snapshot:
        return self.stream.snapshot()

    def station_poses(self) -> list:
        """No base station measured: the plan fits the agent's default area."""
        return []

    def checks(self):
        self.checks_runs += 1
        yield CheckResult(CheckKey.IDENTITY, CheckStatus.PASSED, "cf-lab")
        if self.check_failure is not None:
            yield self.check_failure
            raise ChecksFailed(self.check_failure)
        yield CheckResult(CheckKey.ESTIMATE, CheckStatus.PASSED, "steady")
        return self.report

    def health_test(self):
        if self.health_error:
            raise self.health_error
        return self.health_result

    def guarded_flight(self, report, **kwargs):
        return self.flight

    def manual(self, report):
        return self.manual_controller

    def restore_estimator(self) -> None:
        self.estimator_restored = True

    # standby
    def identify(self):
        return "cf-lab"

    def on_lost(self, callback) -> None:
        self.lose = callback

    def manual_guard(self, report, **kwargs):
        verdict = self.guard_verdict
        return SimpleNamespace(check=lambda snap, now: verdict or Verdict(GuardAction.OK))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
    cloud = FakeCloud()
    cloud.sign_in = lambda email, password: Operator("user-1", email, "Ada", "operator")
    cloud.sign_out = lambda: None
    link = FakeLink()
    events: list[tuple[str, dict]] = []
    outbox = Outbox(tmp_path / "outbox")
    session = Session(cloud=cloud, outbox=outbox, link_factory=lambda: link,
                      publish=lambda kind, payload: events.append((kind, payload)))
    return SimpleNamespace(session=session, link=link, cloud=cloud, outbox=outbox, events=events)


def sign_in(rig):
    return rig.session.sign_in("ada@example.com", "pw")


def start_and_confirm(rig, mode: Mode | None = None):
    """Sign in, run the checks, confirm.

    `mode` is set BEFORE start because the session records the mode it opened
    in: setting it afterwards leaves a manual entry in the history and the
    session lists under both. Sessions now open in MANUAL (Snapshot.mode), so a
    test that wants an Auto session has to say so here.
    """
    sign_in(rig)
    if mode is not None:
        rig.session.set_mode(mode)
    rig.session.start()
    rig.session.wait_idle()
    rig.session.confirm_area()


def run_program(rig, **kwargs):
    # A program needs AUTO, and the session now starts in MANUAL (Snapshot.mode
    # — Auto cannot fly without a position). Every test that runs a program is
    # a test ABOUT Auto, so it says so here rather than inheriting a default.
    rig.session.set_mode(Mode.AUTO)
    rig.session.run_program(**kwargs)
    rig.session.wait_idle()


class TestSignIn:
    def test_an_operator_can_sign_in(self, rig):
        operator = sign_in(rig)
        assert operator.email == "ada@example.com"
        assert rig.session.snapshot().state is State.IDLE

    def test_a_viewer_cannot_operate(self, rig):
        rig.cloud.sign_in = lambda e, p: Operator("u", e, None, "viewer")
        with pytest.raises(SessionError, match="not fly"):
            sign_in(rig)
        assert rig.session.snapshot().state is State.SIGNED_OUT

    def test_bad_credentials_surface_the_message(self, rig):
        def refuse(email, password):
            raise AuthError("Could not sign in as x. Check the email and password.")

        rig.cloud.sign_in = refuse
        with pytest.raises(SessionError, match="Check the email"):
            sign_in(rig)

    def test_nothing_flies_without_signing_in(self, rig):
        with pytest.raises(SessionError, match="Sign in"):
            rig.session.start()

    def test_sign_in_is_audited(self, rig):
        sign_in(rig)
        actions = [r.payload["action"] for r in rig.outbox.pending(Kind.AUDIT)]
        assert actions == ["sign_in"]


class TestStart:
    def test_checks_stream_then_await_confirmation(self, rig):
        sign_in(rig)
        rig.session.start()
        rig.session.wait_idle()
        snap = rig.session.snapshot()
        assert snap.state is State.AWAITING_CONFIRMATION
        assert [c["key"] for c in snap.checks] == ["identity", "estimate"]
        assert snap.drone["hardware_id"] == "cf-lab"

    def test_a_failed_check_closes_the_link_and_explains(self, rig):
        rig.link.check_failure = CheckResult(
            CheckKey.POSITIONING, CheckStatus.FAILED,
            "No base station signal is reaching the drone."
        )
        sign_in(rig)
        rig.session.start()
        rig.session.wait_idle()
        snap = rig.session.snapshot()
        assert snap.state is State.CHECKS_FAILED
        assert "No base station signal" in snap.message
        assert rig.link.closed

    def test_no_drone_is_a_readable_message(self, rig):
        rig.link.open_error = LinkError("No drone answered.")
        sign_in(rig)
        rig.session.start()
        rig.session.wait_idle()
        assert rig.session.snapshot().state is State.CHECKS_FAILED
        assert "No drone answered" in rig.session.snapshot().message

    def test_a_drone_parked_outside_the_flight_area_is_refused(self, rig):
        rig.link.report = ReadyReport(
            hardware_id="cf-lab", vbat=4.0, endurance_s=200, ground_z_m=0.0,
            takeoff_xy=(3.5, 0.0), estimate_spread_m=0.005, positioning=REPORT.positioning,
        )
        sign_in(rig)
        rig.session.start()
        rig.session.wait_idle()
        assert rig.session.snapshot().state is State.CHECKS_FAILED
        assert "outside" in rig.session.snapshot().message
        assert rig.link.closed

    def test_the_drone_and_session_are_recorded_locally(self, rig):
        start_and_confirm(rig)
        assert [r.id for r in rig.outbox.pending(Kind.DRONE)] == ["cf-lab"]
        session_record = next(rig.outbox.pending(Kind.SESSION)).payload
        assert session_record["operator_id"] == "user-1"
        assert session_record["drone_hardware_id"] == "cf-lab"


class TestFlyingRequiresConfirmation:
    def test_a_program_needs_the_area_confirmed(self, rig):
        sign_in(rig)
        rig.session.start()
        rig.session.wait_idle()
        with pytest.raises(SessionError, match="confirm the area"):
            rig.session.run_program()

    def test_the_prop_test_needs_the_area_confirmed(self, rig):
        sign_in(rig)
        rig.session.start()
        rig.session.wait_idle()
        with pytest.raises(SessionError, match="confirm the area"):
            rig.session.prop_test()

    def test_after_confirming_the_state_is_ready(self, rig):
        start_and_confirm(rig)
        assert rig.session.snapshot().state is State.READY
        assert rig.session.snapshot().can_fly


class TestHealthTest:
    def result(self, *, failed=(), battery_passed=True, sag=0.2, battery=True):
        return SimpleNamespace(
            ok=not failed and battery and battery_passed,
            motors=SimpleNamespace(failed=failed),
            battery=SimpleNamespace(passed=battery_passed, sag_v=sag) if battery else None,
            battery_error=None if battery else "The battery test did not report (TimeoutError).",
            to_dict=lambda: {"ok": False},
        )

    def test_a_passing_test_returns_to_ready(self, rig):
        start_and_confirm(rig)
        rig.session.health_test()
        rig.session.wait_idle()
        assert rig.session.snapshot().state is State.READY
        assert rig.session.snapshot().health_test == {"ok": True}
        assert rig.session.snapshot().message is None

    def test_a_failing_motor_is_reported_in_words(self, rig):
        start_and_confirm(rig)
        rig.link.health_result = self.result(failed=(3,))
        rig.session.health_test()
        rig.session.wait_idle()
        assert "Motor(s) 3" in rig.session.snapshot().message

    def test_a_sagging_battery_is_reported_with_the_sag(self, rig):
        start_and_confirm(rig)
        rig.link.health_result = self.result(battery_passed=False, sag=0.61)
        rig.session.health_test()
        rig.session.wait_idle()
        assert "sagged 0.61 V" in rig.session.snapshot().message

    def test_a_battery_half_with_no_result_says_so(self, rig):
        start_and_confirm(rig)
        rig.link.health_result = self.result(battery=False)
        rig.session.health_test()
        rig.session.wait_idle()
        assert "did not report" in rig.session.snapshot().message

    def test_the_old_prop_test_name_runs_the_full_test(self, rig):
        start_and_confirm(rig)
        rig.session.prop_test()
        rig.session.wait_idle()
        assert rig.session.snapshot().health_test == {"ok": True}

    def test_it_is_audited(self, rig):
        start_and_confirm(rig)
        rig.session.health_test()
        rig.session.wait_idle()
        actions = [r.payload["action"] for r in rig.outbox.pending(Kind.AUDIT)]
        assert "health_test" in actions


class TestProgram:
    def test_a_completed_hover_test_records_the_flight(self, rig, monkeypatch):
        monkeypatch.setattr(
            "dronedeck.session.run_hover_test",
            lambda flight, program: ProgramResult(Outcome.COMPLETED, Reason.NONE, "done"),
        )
        start_and_confirm(rig, Mode.AUTO)
        run_program(rig, height_m=0.3, hold_s=5)

        assert rig.session.snapshot().state is State.READY
        flight = next(rig.outbox.pending(Kind.FLIGHT)).payload
        assert flight["mode"] == "auto" and flight["program"] == "hover-test"
        assert flight["status"] == "completed" and flight["ended_at"]
        assert flight["created_by"] == "user-1"
        assert flight["csv_path"].endswith(".csv")

    def test_a_completed_hover_test_records_no_abort_reason(self, rig, monkeypatch):
        """str(Reason.NONE) is "none", and every reader takes a non-empty reason
        as an abort: the Sessions page showed "Aborted: none" (2026-11-20)."""
        monkeypatch.setattr(
            "dronedeck.session.run_hover_test",
            lambda flight, program: ProgramResult(Outcome.COMPLETED, Reason.NONE, "done"),
        )
        start_and_confirm(rig, Mode.AUTO)
        run_program(rig, height_m=0.3, hold_s=5)
        flight = next(rig.outbox.pending(Kind.FLIGHT)).payload
        assert flight["abort_reason"] is None
        assert rig.session.history is not None
        assert rig.session.history.meta.flights[-1].abort_reason is None

    def test_a_locked_drone_is_refused_before_anything_arms(self, rig, monkeypatch):
        """2026-11-20, flight f86b8c62: right after a mission the supervisor
        read 580 (LOCKED), the hover test flew with thrust 0, and the height
        guard ended it as if it had flown badly."""
        flown: list = []
        monkeypatch.setattr(
            "dronedeck.session.run_hover_test",
            lambda flight, program: flown.append(program) or ProgramResult(
                Outcome.COMPLETED, Reason.NONE, "done"),
        )
        start_and_confirm(rig, Mode.AUTO)
        rig.link.stream.snapshot = lambda: Snapshot(
            MappingProxyType({"supervisor.info": 580}), 1.0)
        assert [b["code"] for b in rig.session.program_blockers()] == ["motors"]
        assert "Reset drone" in rig.session.program_blockers()[0]["fix"]
        rig.session.set_mode(Mode.AUTO)
        with pytest.raises(SessionError, match="locked its motors"):
            rig.session.run_program()
        assert flown == []
        assert rig.session.snapshot().state is State.READY

    def test_a_ready_drone_has_no_program_blockers(self, rig):
        start_and_confirm(rig, Mode.AUTO)
        rig.link.stream.snapshot = lambda: Snapshot(
            MappingProxyType({"supervisor.info": 526}), 1.0)
        assert rig.session.program_blockers() == []

    def test_a_guard_abort_is_recorded_as_aborted(self, rig, monkeypatch):
        monkeypatch.setattr(
            "dronedeck.session.run_hover_test",
            lambda flight, program: ProgramResult(
                Outcome.LANDED_BY_GUARD, Reason.BATTERY_LOW, "Battery low — landing."),
        )
        start_and_confirm(rig, Mode.AUTO)
        run_program(rig)
        flight = next(rig.outbox.pending(Kind.FLIGHT)).payload
        assert flight["status"] == "aborted" and flight["abort_reason"] == "battery_low"
        audit = [r.payload for r in rig.outbox.pending(Kind.AUDIT)
                 if r.payload["action"] == "program_run"]
        assert audit[0]["result"] == "aborted"

    def test_a_flight_longer_than_the_battery_is_refused(self, rig):
        # 20 s of endurance leaves a 16 s budget; this program needs ~36 s.
        rig.link.report = ReadyReport(
            hardware_id="cf-lab", vbat=3.48, endurance_s=20.0, ground_z_m=0.0,
            takeoff_xy=(0.0, 0.0), estimate_spread_m=0.005, positioning=REPORT.positioning,
        )
        start_and_confirm(rig, Mode.AUTO)
        with pytest.raises(SessionError, match="battery"):
            rig.session.run_program(hold_s=30)

    def test_programs_are_auto_mode_only(self, rig):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        with pytest.raises(SessionError, match="Switch to Auto"):
            rig.session.run_program()

    def test_an_unexpected_failure_still_closes_the_flight_record(self, rig, monkeypatch):
        def explode(flight, program):
            raise RuntimeError("radio died")

        monkeypatch.setattr("dronedeck.session.run_hover_test", explode)
        start_and_confirm(rig, Mode.AUTO)
        run_program(rig)
        flight = next(rig.outbox.pending(Kind.FLIGHT)).payload
        assert flight["status"] == "failed" and "radio died" in flight["error"]


class TestManual:
    def test_arming_needs_manual_mode(self, rig):
        # An Auto session: the refusal this test is about.
        start_and_confirm(rig, Mode.AUTO)
        with pytest.raises(SessionError, match="Switch to Manual"):
            rig.session.arm_manual()

    def test_arming_starts_the_controller_and_records_a_flight(self, rig):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.session.arm_manual()
        assert rig.link.manual_controller.events[:2] == ["arm", "start"]
        assert rig.session.snapshot().activity == "manual"
        assert next(rig.outbox.pending(Kind.FLIGHT)).payload["mode"] == "manual"

    def test_the_guard_lands_a_manual_flight_on_low_battery(self, rig):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.link.guard_verdict = Verdict(GuardAction.LAND, Reason.BATTERY_LOW, "Battery low.")
        rig.session.arm_manual()
        deadline = time.monotonic() + 3
        while ("land" not in rig.link.manual_controller.events
               and time.monotonic() < deadline):
            time.sleep(0.05)
        assert "land" in rig.link.manual_controller.events
        reasons = [r.payload["detail"].get("reason") for r in rig.outbox.pending(Kind.AUDIT)
                   if r.payload["action"] == "guard_abort"]
        assert reasons == ["battery_low"]

    def test_mode_cannot_change_mid_flight(self, rig):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.session.arm_manual()
        with pytest.raises(SessionError, match="Finish the current flight"):
            rig.session.set_mode(Mode.AUTO)


class TestOneMode:
    """A session belongs to the mode it started in (2026-10-01): nothing of
    Manual bleeds into an Auto session, or the other way round."""

    def audited_mode_changes(self, rig):
        return [r.payload for r in rig.outbox.pending(Kind.AUDIT)
                if r.payload["action"] == "mode_changed"]

    @pytest.mark.parametrize("opened_in, other", [(Mode.MANUAL, Mode.AUTO),
                                                  (Mode.AUTO, Mode.MANUAL)])
    def test_an_open_session_keeps_its_mode(self, rig, opened_in, other):
        start_and_confirm(rig, opened_in)
        before = len(self.audited_mode_changes(rig))
        with pytest.raises(SessionError, match=f"A {str(opened_in).capitalize()} session "
                                               "is open"):
            rig.session.set_mode(other)
        assert rig.session.snapshot().mode is opened_in
        assert len(self.audited_mode_changes(rig)) == before      # a refusal changes nothing

    def test_it_is_refused_while_waiting_for_the_area_to_be_confirmed(self, rig):
        sign_in(rig)
        rig.session.start()
        rig.session.wait_idle()
        assert rig.session.snapshot().state is State.AWAITING_CONFIRMATION
        with pytest.raises(SessionError, match="session is open"):
            rig.session.set_mode(Mode.AUTO)

    def test_asking_for_the_mode_it_is_already_in_is_harmless(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.set_mode(Mode.MANUAL)
        assert rig.session.snapshot().mode is Mode.MANUAL

    def test_ending_the_session_frees_the_mode(self, rig):
        start_and_confirm(rig, Mode.MANUAL)
        rig.session.end()
        rig.session.set_mode(Mode.AUTO)
        assert rig.session.snapshot().mode is Mode.AUTO

    def test_with_no_session_the_mode_is_free(self, rig):
        sign_in(rig)
        rig.session.set_mode(Mode.AUTO)
        rig.session.set_mode(Mode.MANUAL)
        assert rig.session.snapshot().mode is Mode.MANUAL

    def test_an_auto_session_cannot_arm_the_keys_and_a_manual_one_cannot_run_a_program(
            self, rig):
        start_and_confirm(rig, Mode.AUTO)
        with pytest.raises(SessionError, match="Switch to Manual"):
            rig.session.arm_manual()
        rig.session.end()
        start_and_confirm(rig, Mode.MANUAL)
        with pytest.raises(SessionError, match="Switch to Auto"):
            rig.session.run_program()


class TestLandAndStop:
    def test_land_reaches_the_manual_controller(self, rig):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.session.arm_manual()
        rig.session.land()
        assert "land" in rig.link.manual_controller.events

    def test_emergency_stop_is_audited_as_aborted(self, rig):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.session.arm_manual()
        rig.session.emergency_stop()
        assert "emergency_stop" in rig.link.manual_controller.events
        events = [r.payload for r in rig.outbox.pending(Kind.AUDIT)
                  if r.payload["action"] == "emergency_stop"]
        assert events[0]["result"] == "aborted"


class TestEnd:
    def test_end_lands_closes_and_records(self, rig):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.session.arm_manual()
        rig.session.end()

        assert "stop" in rig.link.manual_controller.events      # lands, then disarms
        assert rig.link.closed
        assert rig.session.snapshot().state is State.IDLE
        session_record = next(rig.outbox.pending(Kind.SESSION)).payload
        assert session_record["ended_at"] and session_record["end_reason"] == "operator"
        assert next(rig.outbox.pending(Kind.FLIGHT)).payload["ended_at"]

    def test_an_ended_flight_is_queued_for_playback_on_other_computers(self, rig):
        # Its plan as flown and control trace (sync/flight_playback.py), once.
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.session.arm_manual()
        rig.session.end()
        flight_id = next(rig.outbox.pending(Kind.FLIGHT)).id
        queued = rig.outbox.get(Kind.PLAYBACK, flight_id)
        assert queued is not None and queued["uploaded_through"] == 0
        assert queued["plan_sent"] is False

    def test_end_without_a_session_is_harmless(self, rig):
        sign_in(rig)
        rig.session.end()
        assert rig.session.snapshot().state is State.IDLE

    def test_sign_out_ends_an_open_session(self, rig):
        start_and_confirm(rig)
        rig.session.sign_out()
        assert rig.link.closed
        assert rig.session.snapshot().state is State.SIGNED_OUT
        actions = [r.payload["action"] for r in rig.outbox.pending(Kind.AUDIT)]
        assert "session_end" in actions and actions[-1] == "sign_out"


class TestUnassisted:
    """Flying with no position estimate.

    The operator standing over the drone decides whether to fly — base stations
    are a recommendation, not a refusal. What must not move: the drone is never
    told it can hold a position, and a preset program is never flown blind.
    That combination is what put a drone into a wall on 2026-09-16.
    """

    @staticmethod
    def unassisted(rig, mode: Mode | None = None):
        rig.link.report = ReadyReport(
            hardware_id="cf-lab", vbat=4.05, endurance_s=200.0, ground_z_m=0.0,
            takeoff_xy=(0.0, 0.0), estimate_spread_m=float("inf"),
            positioning=PositioningStatus((), (), (), (0, 1, 2, 3), (4.38, 4.38, 4.38)),
            assisted=False,
        )
        sign_in(rig)
        if mode is not None:
            rig.session.set_mode(mode)
        rig.session.start()
        rig.session.wait_idle()
        return rig.session.snapshot()

    def test_the_checks_do_not_block_and_the_state_says_unassisted(self, rig):
        snapshot = self.unassisted(rig)
        assert snapshot.state is State.AWAITING_CONFIRMATION
        assert snapshot.assisted is False
        assert "No base station signal" in (snapshot.unassisted_reason or "")

    def test_confirming_needs_the_second_acknowledgement(self, rig):
        self.unassisted(rig)
        with pytest.raises(SessionError, match="flying it by eye"):
            rig.session.confirm_area()
        assert rig.session.snapshot().state is State.AWAITING_CONFIRMATION

        rig.session.confirm_area(accept_unassisted=True)
        assert rig.session.snapshot().state is State.READY

    def test_a_program_is_still_refused_with_a_reason(self, rig):
        self.unassisted(rig, Mode.AUTO)
        rig.session.confirm_area(accept_unassisted=True)
        with pytest.raises(SessionError, match="fly blind"):
            rig.session.run_program()

    def test_the_reason_is_kept_with_the_session(self, rig):
        """A session that flew on the barometer says why afterwards (2026-10-24:
        one did, and nothing on disk said why)."""
        self.unassisted(rig)
        meta = rig.session.history.meta
        assert meta.assisted is False
        assert "No base station signal" in (meta.unassisted_reason or "")
        saved = (rig.session.history.folder / "meta.json").read_text()
        assert "No base station signal" in saved

    def test_checking_again_with_the_stations_seen_clears_it(self, rig):
        self.unassisted(rig)
        rig.link.report = REPORT
        rig.session.retry()
        rig.session.wait_idle()
        meta = rig.session.history.meta
        assert (meta.assisted, meta.unassisted_reason) == (True, None)

    def test_manual_flight_is_allowed(self, rig):
        self.unassisted(rig)
        rig.session.confirm_area(accept_unassisted=True)
        rig.session.set_mode(Mode.MANUAL)
        rig.session.arm_manual()
        assert "arm" in rig.link.manual_controller.events
        assert rig.session.snapshot().activity == "manual"

    def test_a_drone_parked_outside_the_fence_is_not_refused(self, rig):
        """There is no fence without a position: those coordinates are the
        accelerometer integrating, not metres from an origin."""
        rig.link.report = ReadyReport(
            hardware_id="cf-lab", vbat=4.05, endurance_s=200.0, ground_z_m=0.0,
            takeoff_xy=(99.0, -99.0), estimate_spread_m=float("inf"),
            positioning=PositioningStatus((), (), (), (), (None, None, None)),
            assisted=False,
        )
        sign_in(rig)
        rig.session.start()
        rig.session.wait_idle()
        assert rig.session.snapshot().state is State.AWAITING_CONFIRMATION

    def test_an_assisted_session_still_confirms_in_one_step(self, rig):
        sign_in(rig)
        rig.session.start()
        rig.session.wait_idle()
        assert rig.session.snapshot().assisted is True
        rig.session.confirm_area()
        assert rig.session.snapshot().state is State.READY


def wait_for(condition, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class TestManualFinishes:
    """A manual flight that comes down gives the session back.

    In the lab (2026-09-16) a tumble stopped the motors and the app stayed
    "busy" for good: no End session, no mode switch, no second attempt.
    """

    def fly(self, rig, verdict=None):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.link.guard_verdict = verdict
        rig.session.arm_manual()
        assert rig.session.snapshot().state is State.BUSY

    def test_landing_returns_the_session_to_ready(self, rig):
        self.fly(rig)
        rig.link.manual_controller.state = "landed"
        assert wait_for(lambda: rig.session.snapshot().state is State.READY)
        snapshot = rig.session.snapshot()
        assert snapshot.activity is None
        assert rig.session.manual is None
        flights = [r.payload for r in rig.outbox.pending(Kind.FLIGHT)]
        assert flights[-1]["outcome"] == "landed"

    def test_a_tumble_frees_the_session_and_records_the_abort(self, rig):
        self.fly(rig, Verdict(GuardAction.STOP, Reason.TUMBLED, "Tumbled."))
        assert wait_for(lambda: "emergency_stop" in rig.link.manual_controller.events)
        rig.link.manual_controller.state = "stopped"
        assert wait_for(lambda: rig.session.snapshot().state is State.READY)
        flights = [r.payload for r in rig.outbox.pending(Kind.FLIGHT)]
        assert flights[-1]["status"] == "aborted"
        assert flights[-1]["abort_reason"] == "tumbled"
        # The session is free again — but it is still a Manual session
        # (sessions-and-modes.txt): the mode changes only once it has ended.
        with pytest.raises(SessionError, match="A Manual session is open"):
            rig.session.set_mode(Mode.AUTO)
        rig.session.end()
        rig.session.set_mode(Mode.AUTO)

    def test_low_battery_lands_and_then_ends_the_session(self, rig):
        self.fly(rig, Verdict(GuardAction.LAND, Reason.BATTERY_LOW, "Battery low."))
        assert wait_for(lambda: "land" in rig.link.manual_controller.events)
        rig.link.manual_controller.state = "landed"
        assert wait_for(lambda: rig.session.snapshot().state is State.IDLE)
        assert "battery" in (rig.session.snapshot().message or "").lower()
        ends = [r.payload["detail"].get("reason") for r in rig.outbox.pending(Kind.AUDIT)
                if r.payload["action"] == "session_end"]
        assert ends == ["battery_low"]

    def test_end_session_while_flying_still_works(self, rig):
        self.fly(rig)
        rig.session.end()
        assert rig.session.snapshot().state is State.IDLE
        assert "stop" in rig.link.manual_controller.events


class TestRetry:
    """After a crash, the same session checks the drone again before anything flies.

    2026-09-17: a flight tumbled, the session went straight back to "fly
    again", and the next flight reported flying for 19 s with thrust 0 — the
    firmware was still holding the motors. Retry re-runs every check (which
    now includes recovering the motors) in the same session.
    """

    def crash(self, rig):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.link.guard_verdict = Verdict(GuardAction.STOP, Reason.TUMBLED, "Tumbled.")
        rig.session.arm_manual()
        assert wait_for(lambda: "emergency_stop" in rig.link.manual_controller.events)
        rig.link.manual_controller.state = "stopped"
        assert wait_for(lambda: rig.session.snapshot().state is State.READY)
        rig.link.guard_verdict = None

    def test_a_tumble_requires_a_retry_before_flying_again(self, rig):
        self.crash(rig)
        snap = rig.session.snapshot()
        assert snap.retry_required
        assert "Retry" in snap.message
        rig.link.manual_controller = FakeManual()
        with pytest.raises(SessionError, match="Press Retry"):
            rig.session.arm_manual()

    def test_the_battery_and_motor_test_waits_for_the_retry_too(self, rig):
        self.crash(rig)
        with pytest.raises(SessionError, match="Press Retry"):
            rig.session.health_test()

    def test_an_emergency_stop_requires_a_retry(self, rig):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.session.arm_manual()
        rig.session.emergency_stop()
        rig.link.manual_controller.state = "stopped"
        assert wait_for(lambda: rig.session.snapshot().state is State.READY)
        assert rig.session.snapshot().retry_required

    def test_a_normal_landing_does_not(self, rig):
        start_and_confirm(rig)
        rig.session.set_mode(Mode.MANUAL)
        rig.session.arm_manual()
        rig.link.manual_controller.state = "landed"
        assert wait_for(lambda: rig.session.snapshot().state is State.READY)
        assert not rig.session.snapshot().retry_required

    def test_retry_runs_every_check_again_in_the_same_session(self, rig):
        self.crash(rig)
        session_id = rig.session.snapshot().session_id
        runs = rig.link.checks_runs
        rig.session.retry()
        rig.session.wait_idle()
        snap = rig.session.snapshot()
        assert rig.link.checks_runs == runs + 1
        assert snap.state is State.AWAITING_CONFIRMATION     # confirm again, as new
        assert snap.session_id == session_id
        assert not snap.retry_required
        rig.session.confirm_area()
        rig.link.manual_controller = FakeManual()
        rig.session.arm_manual()                             # and it may fly
        assert rig.session.snapshot().state is State.BUSY

    def test_a_failed_retry_keeps_the_session_and_can_be_retried(self, rig):
        self.crash(rig)
        session_id = rig.session.snapshot().session_id
        rig.link.check_failure = CheckResult(
            CheckKey.MOTORS, CheckStatus.FAILED,
            "The drone has locked its motors and needs a restart.")
        rig.session.retry()
        rig.session.wait_idle()
        snap = rig.session.snapshot()
        assert snap.state is State.CHECKS_FAILED
        assert snap.session_id == session_id
        assert snap.retry_required
        assert "restart" in snap.message
        assert rig.link.closed                               # a power-cycled drone needs a new link

        rig.link.check_failure = None
        rig.session.retry()
        rig.session.wait_idle()
        assert rig.session.snapshot().state is State.AWAITING_CONFIRMATION
        assert rig.link.is_open

    def test_start_is_refused_while_the_session_is_still_open(self, rig):
        self.crash(rig)
        rig.link.check_failure = CheckResult(CheckKey.MOTORS, CheckStatus.FAILED, "locked")
        rig.session.retry()
        rig.session.wait_idle()
        with pytest.raises(SessionError, match="Press Retry"):
            rig.session.start()

    def test_retry_needs_a_session(self, rig):
        sign_in(rig)
        with pytest.raises(SessionError, match="no session"):
            rig.session.retry()

    def test_retry_is_audited(self, rig):
        self.crash(rig)
        rig.session.retry()
        rig.session.wait_idle()
        retries = [r.payload for r in rig.outbox.pending(Kind.AUDIT)
                   if r.payload["action"] == "session_retry"]
        assert retries and retries[0]["detail"] == {"after_abnormal_end": True}

    def test_an_aborted_program_requires_a_retry(self, rig, monkeypatch):
        monkeypatch.setattr(
            "dronedeck.session.run_hover_test",
            lambda flight, program: ProgramResult(Outcome.ABORTED, Reason.TUMBLED, "Tumbled."),
        )
        start_and_confirm(rig, Mode.AUTO)
        run_program(rig, height_m=0.3, hold_s=5)
        assert rig.session.snapshot().retry_required

    def test_ending_the_session_clears_it(self, rig):
        self.crash(rig)
        rig.session.end()
        assert not rig.session.snapshot().retry_required


class TestHistory:
    def test_a_session_leaves_a_record_with_its_operator_and_times(self, rig):
        from dronedeck import history

        start_and_confirm(rig)
        session_id = rig.session.snapshot().session_id
        rig.session._publish_telemetry(Snapshot(MappingProxyType(
            {"pm.vbat": 4.01, "baro.temp": 29.5, "stateEstimate.z": 0.412}), 1.0))
        rig.session.end()

        found = history.list_sessions()
        assert [s["id"] for s in found] == [session_id]
        record = found[0]
        assert record["operator_email"] == "ada@example.com"
        assert record["ended_at"] is not None and record["duration_s"] is not None
        assert record["summary"]["battery_start_v"] == 4.01
        assert record["summary"]["max_height_m"] == pytest.approx(0.4)

    def test_samples_are_readable_by_variable(self, rig):
        from dronedeck import history

        start_and_confirm(rig)
        session_id = rig.session.snapshot().session_id
        rig.session._publish_telemetry(Snapshot(MappingProxyType(
            {"pm.vbat": 3.95, "baro.temp": 30.1}), 1.0))
        rows = history.read_samples(session_id, ["pm.vbat", "not.a.variable"])
        assert rows[0]["pm.vbat"] == 3.95
        assert rows[0]["not.a.variable"] is None

    def test_an_old_none_abort_reason_reads_as_no_reason(self, tmp_path):
        """Records saved before 2026-11-20 carry abort_reason "none" for a
        completed hover test; they read as no reason, and are not rewritten.
        A real reason is kept."""
        import json

        from dronedeck import history

        folder = tmp_path / "s-old"
        folder.mkdir()
        meta = {
            "id": "s-old", "operator_id": "u", "operator_email": "a@b.c",
            "operator_name": None, "drone_hardware_id": None, "mode": "auto",
            "assisted": True, "started_at": "2026-11-20T20:55:20+00:00",
            "flights": [
                {"id": "f1", "mode": "auto", "program": "hover-test",
                 "started_at": "2026-11-20T20:55:32+00:00", "outcome": "completed",
                 "abort_reason": "none"},
                {"id": "f2", "mode": "auto", "program": "hover-test",
                 "started_at": "2026-11-20T20:09:16+00:00", "outcome": "landed_by_guard",
                 "abort_reason": "height_error"},
            ],
        }
        written = json.dumps(meta)
        (folder / "meta.json").write_text(written)

        record = history.read_session("s-old", root=tmp_path)
        assert record is not None
        assert [f["abort_reason"] for f in record["flights"]] == [None, "height_error"]
        assert (folder / "meta.json").read_text() == written

    def test_a_session_id_cannot_walk_out_of_the_folder(self):
        from dronedeck import history

        with pytest.raises(ValueError):
            history.read_samples("../../etc", ["x"])


class TestStaySignedIn:
    def test_signing_in_saves_the_token_and_signing_out_clears_it(self, rig, tmp_path):
        from dronedeck.sync import auth_store

        rig.cloud.refresh_token = "refresh-abc"
        sign_in(rig)
        assert auth_store.load() == ("refresh-abc", "ada@example.com")
        import stat
        mode = stat.S_IMODE((tmp_path / auth_store.FILENAME).stat().st_mode)
        assert mode == 0o600
        rig.session.sign_out()
        assert auth_store.load() is None

    def test_a_saved_token_signs_back_in_at_startup(self, rig):
        from dronedeck.sync import auth_store

        auth_store.save("refresh-abc", "ada@example.com")
        rig.cloud.refresh_token = "refresh-rotated"
        rig.cloud.restore = lambda token: Operator("user-1", "ada@example.com", "Ada", "operator")
        operator = rig.session.restore_sign_in()
        assert operator is not None
        assert rig.session.snapshot().state is State.IDLE
        assert auth_store.load() == ("refresh-rotated", "ada@example.com")

    def test_an_expired_token_is_forgotten(self, rig):
        from dronedeck.sync import auth_store

        auth_store.save("stale", "ada@example.com")

        def refuse(token):
            raise AuthError("The saved sign-in has expired. Please sign in again.")

        rig.cloud.restore = refuse
        assert rig.session.restore_sign_in() is None
        assert rig.session.snapshot().state is State.SIGNED_OUT
        assert auth_store.load() is None

    def test_a_slow_network_keeps_the_saved_sign_in(self, rig):
        """A timeout says nothing about the token. Treating it as "expired"
        deleted the saved sign-in because Supabase was slow to answer."""
        from dronedeck.sync import auth_store
        from dronedeck.sync.cloud import CloudTimeout

        auth_store.save("refresh-abc", "ada@example.com")

        def slow(token):
            raise CloudTimeout("Supabase did not answer within 25 seconds.")

        rig.cloud.restore = slow
        assert rig.session.restore_sign_in() is None
        snapshot = rig.session.snapshot()
        assert snapshot.state is State.SIGNED_OUT and not snapshot.restoring
        assert "did not answer" in (snapshot.message or "")
        assert auth_store.load() == ("refresh-abc", "ada@example.com")


class TestHistoryByMode:
    """Auto and Manual history are kept apart — per reading, not per session."""

    def test_a_session_recorded_in_two_modes_lists_under_both_and_splits_its_readings(self, rig):
        """Sessions from before 2026-10-01 could switch mode mid-session; the
        history still reads them. A session cannot switch now (TestOneMode)."""
        from dronedeck import history

        start_and_confirm(rig, Mode.AUTO)
        session_id = rig.session.snapshot().session_id
        log_ = rig.session.history
        log_._clock = lambda: 0.0                       # sample now
        rig.session._publish_telemetry(Snapshot(MappingProxyType({"pm.vbat": 4.10}), 1.0))
        log_.set_mode("manual")
        log_._clock = lambda: 5.0                       # the next second
        rig.session._publish_telemetry(Snapshot(MappingProxyType({"pm.vbat": 4.00}), 2.0))
        rig.session.end()

        assert [s["id"] for s in history.list_sessions(mode="auto")] == [session_id]
        assert [s["id"] for s in history.list_sessions(mode="manual")] == [session_id]
        assert history.read_session(session_id)["modes"] == ["auto", "manual"]

        auto = history.read_samples(session_id, ["pm.vbat"], mode="auto")
        manual = history.read_samples(session_id, ["pm.vbat"], mode="manual")
        assert [r["pm.vbat"] for r in auto] == [4.10]
        assert [r["pm.vbat"] for r in manual] == [4.00]

    def test_a_session_only_in_auto_is_not_listed_under_manual(self, rig):
        from dronedeck import history

        start_and_confirm(rig, Mode.AUTO)
        rig.session.end()
        assert history.list_sessions(mode="manual") == []
        assert len(history.list_sessions(mode="auto")) == 1


class TestStartupState:
    """The app must not show "signed out" before the saved sign-in was tried."""

    def test_a_fresh_session_reports_restoring(self, rig):
        assert rig.session.snapshot().restoring is True

    def test_restoring_clears_when_there_is_nothing_saved(self, rig):
        assert rig.session.restore_sign_in() is None
        assert rig.session.snapshot().restoring is False

    def test_restoring_clears_after_a_successful_restore(self, rig):
        from dronedeck.sync import auth_store

        auth_store.save("refresh-abc", "ada@example.com")
        rig.cloud.restore = lambda token: Operator("user-1", "ada@example.com", "Ada", "operator")
        rig.session.restore_sign_in()
        snapshot = rig.session.snapshot()
        assert snapshot.restoring is False and snapshot.state is State.IDLE

    def test_restoring_clears_when_the_saved_sign_in_expired(self, rig):
        from dronedeck.sync import auth_store

        auth_store.save("stale", "ada@example.com")

        def refuse(token):
            raise AuthError("expired")

        rig.cloud.restore = refuse
        rig.session.restore_sign_in()
        assert rig.session.snapshot().restoring is False

    def test_signing_in_by_hand_also_clears_it(self, rig):
        sign_in(rig)
        assert rig.session.snapshot().restoring is False


class TestWhatIsRunning:
    """"Something is already running" was true and useless.

    On 2026-09-22 a connect that could not time out held the worker for ever,
    and the operator was told to wait for a thing the message would not name.
    """

    def test_the_busy_message_names_the_step_and_the_wait(self, rig):
        import threading as _t

        session = rig.session
        release = _t.Event()
        session._start_worker("checks", release.wait)
        try:
            with pytest.raises(SessionError) as caught:
                session._start_worker("manual arm", lambda: None)
            text = str(caught.value)
            assert "Checks" in text                  # names what is running
            assert "End session" in text             # and the way out
        finally:
            release.set()
            session.wait_idle(timeout=5)


# ── standby: the drone connected with no session ─────────────────────────


@pytest.fixture
def standby(tmp_path, monkeypatch):
    """A session whose link opens count, and whose Wi-Fi hand-off is recorded.

    The loop thread is never started: `_standby_tick` is driven directly, so
    each test says exactly when the agent looks for the drone.
    """
    monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
    cloud = FakeCloud()
    cloud.sign_in = lambda email, password: Operator("user-1", email, "Ada", "operator")
    cloud.sign_out = lambda: None
    link = FakeLink()
    opens: list[int] = []
    real_open = link.open

    def counting_open() -> None:
        opens.append(1)
        real_open()

    link.open = counting_open                      # type: ignore[method-assign]
    events: list[tuple[str, dict]] = []
    session = Session(cloud=cloud, outbox=Outbox(tmp_path / "outbox"),
                      link_factory=lambda: link,
                      publish=lambda kind, payload: events.append((kind, payload)))
    return SimpleNamespace(session=session, link=link, opens=opens, events=events)


class TestStandby:
    def test_nothing_is_opened_while_signed_out(self, standby):
        standby.session._standby_tick()
        assert standby.opens == []
        assert standby.session.snapshot().radio["state"] == "off"

    def test_signed_in_and_idle_it_connects_and_says_which_drone(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.session._standby_tick()
        snap = standby.session.snapshot()
        assert standby.link.is_open
        assert snap.radio == {"state": "connected", "hardware_id": "cf-lab", "message": None}
        assert snap.state is State.IDLE          # standby is not a session

    def test_vitals_stream_but_nothing_is_recorded(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.session._standby_tick()
        assert standby.link.stream.subscribers, "vitals are published"
        standby.link.stream.subscribers[0](standby.link.stream.snapshot())
        assert any(kind == "telemetry" for kind, _ in standby.events)
        assert standby.session.history is None   # a file only ever comes from a session

    def test_a_session_takes_the_link_over_and_still_runs_every_check(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.session._standby_tick()
        standby.session.start()
        standby.session.wait_idle()
        assert standby.opens == [1]              # the standby link, not a second one
        assert standby.link.checks_runs == 1
        assert standby.session.snapshot().state is State.AWAITING_CONFIRMATION

    def test_standby_never_arms_or_tests_the_motors(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.session._standby_tick()
        assert standby.link.flight.landed is False and standby.link.flight.stopped is False
        assert standby.link.manual_controller.events == []
        assert standby.link.checks_runs == 0     # not even the checks
        assert standby.session.snapshot().can_fly is False

    def test_it_leaves_the_radio_alone_while_a_session_owns_it(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.session.start()
        standby.session.wait_idle()
        opens = len(standby.opens)
        standby.link.is_open = False             # even with the link down
        standby.session._standby_tick()
        assert len(standby.opens) == opens

    def test_after_a_session_ends_it_connects_again(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.session.start()
        standby.session.wait_idle()
        standby.session.end()
        standby.session.wait_idle()
        assert not standby.link.is_open          # the session let go
        standby.session._standby_tick()
        assert standby.link.is_open
        assert standby.session.snapshot().radio["state"] == "connected"

    def test_no_drone_found_says_why(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.link.open_error = LinkError("No Crazyradio found.")
        standby.session._standby_tick()
        radio = standby.session.snapshot().radio
        assert radio["state"] == "searching"
        assert "Crazyradio" in radio["message"]

    def test_a_drone_switched_off_is_released_and_looked_for_again(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.session._standby_tick()
        standby.link.lose("Too many packets lost")
        assert wait_for(lambda: standby.session.snapshot().radio["state"] == "searching")
        assert standby.session.link is None
        assert "Too many packets lost" in standby.session.snapshot().radio["message"]

    def test_disconnect_frees_the_radio_until_connect(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.session._standby_tick()
        standby.session.disconnect_drone()
        assert not standby.link.is_open
        assert standby.session.snapshot().radio["state"] == "paused"
        standby.session._standby_tick()
        assert not standby.link.is_open          # stays free
        standby.session.connect_drone()
        standby.session._standby_tick()
        assert standby.link.is_open

    def test_disconnect_is_refused_mid_session(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.session.start()
        standby.session.wait_idle()
        with pytest.raises(SessionError):
            standby.session.disconnect_drone()

    def test_signing_out_releases_the_drone(self, standby):
        standby.session.sign_in("ada@example.com", "pw")
        standby.session._standby_tick()
        standby.session.sign_out()
        assert not standby.link.is_open
        assert standby.session.snapshot().radio["state"] == "off"


class TestRestartDrone:
    """The one supported way to make the deck rejoin and say its address."""

    def make(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
        cloud = FakeCloud()
        cloud.sign_in = lambda email, password: Operator("user-1", email, "Ada", "operator")
        cloud.sign_out = lambda: None
        link = FakeLink()
        cycled: list[str] = []
        session = Session(cloud=cloud, outbox=Outbox(tmp_path / "outbox"),
                          link_factory=lambda: link, power_cycle=cycled.append)
        session.sign_in("ada@example.com", "pw")
        return session, link, cycled

    def test_on_standby_it_closes_the_link_and_restarts_over_the_radio(self, tmp_path, monkeypatch):
        session, link, cycled = self.make(tmp_path, monkeypatch)
        session._standby_tick()
        session.restart_drone(reason="test")
        assert cycled == ["radio://0/80/2M"]
        assert not link.is_open
        assert session.snapshot().radio["state"] == "restarting"

    def test_standby_waits_for_the_drone_to_boot(self, tmp_path, monkeypatch):
        """Connecting while it boots blocks for the whole connect timeout."""
        session, link, _ = self.make(tmp_path, monkeypatch)
        session.restart_drone(reason="test")
        session._standby_tick()
        assert not link.is_open
        session._standby_not_before = 0.0
        session._standby_tick()
        assert link.is_open

    def test_never_during_a_session(self, tmp_path, monkeypatch):
        session, _, cycled = self.make(tmp_path, monkeypatch)
        session.start()
        session.wait_idle()
        with pytest.raises(SessionError):
            session.restart_drone(reason="test")
        assert cycled == []


class TestResetDrone:
    """The hard switch (2026-09-30): after a crash, stop everything, restart the
    drone, and stand exactly where a new session stands after its checks."""

    def make(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
        cloud = FakeCloud()
        cloud.sign_in = lambda email, password: Operator("user-1", email, "Ada", "operator")
        cloud.sign_out = lambda: None
        link = FakeLink()
        cycled: list[str] = []
        outbox = Outbox(tmp_path / "outbox")
        session = Session(cloud=cloud, outbox=outbox, link_factory=lambda: link,
                          power_cycle=cycled.append)
        session._reset_settle_s = 0.0
        rig = SimpleNamespace(session=session, link=link, outbox=outbox, cycled=cycled)
        return rig

    @staticmethod
    def flying(rig):
        """Mid-flight, with a controller that stops the way the real one does:
        at once, on the spot (ManualController.emergency_stop)."""
        start_and_confirm(rig)
        manual = rig.link.manual_controller
        manual.state = "flying"

        def stop_now():
            manual.events.append("emergency_stop")
            manual.state = "stopped"

        manual.emergency_stop = stop_now
        rig.session.arm_manual()
        assert rig.session.snapshot().state is State.BUSY
        return manual

    def test_mid_flight_it_stops_restarts_and_checks_again(self, tmp_path, monkeypatch):
        rig = self.make(tmp_path, monkeypatch)
        manual = self.flying(rig)
        session_id = rig.session.snapshot().session_id
        runs = rig.link.checks_runs

        rig.session.reset_drone()
        assert manual.events.count("emergency_stop") == 1   # before anything waited
        rig.session.wait_idle()

        snap = rig.session.snapshot()
        assert rig.cycled == ["radio://0/80/2M"]
        assert rig.link.checks_runs == runs + 1
        assert snap.state is State.AWAITING_CONFIRMATION     # confirm again, as new
        assert snap.session_id == session_id                 # the same session
        assert not snap.retry_required
        assert snap.activity is None
        assert snap.message.startswith("Restarted and checked again.")
        assert rig.session.manual is None
        flights = [r.payload for r in rig.outbox.pending(Kind.FLIGHT)]
        assert flights[-1]["status"] == "aborted"
        assert flights[-1]["abort_reason"] == "emergency_stop"
        actions = [r.payload["action"] for r in rig.outbox.pending(Kind.AUDIT)]
        assert "drone_reset" in actions

    def test_after_it_the_drone_may_fly_again(self, tmp_path, monkeypatch):
        rig = self.make(tmp_path, monkeypatch)
        self.flying(rig)
        rig.session.reset_drone()
        rig.session.wait_idle()
        rig.session.confirm_area()
        rig.link.manual_controller = FakeManual()
        rig.session.arm_manual()
        assert rig.session.snapshot().state is State.BUSY

    def test_the_guard_closing_the_flight_first_cannot_undo_the_reset(
            self, tmp_path, monkeypatch):
        """The guard sees "stopped" and closes the flight itself. Caught mid-close
        — it has taken the controller, not yet said "Motors stopped" — the reset
        must wait for it, so that state lands before the reset's, not after."""
        rig = self.make(tmp_path, monkeypatch)
        manual = self.flying(rig)
        release = threading.Event()
        in_close = threading.Event()

        def slow_stop():
            in_close.set()
            release.wait(5)

        manual.stop = slow_stop
        manual.emergency_stop()                              # the guard's next look closes it
        assert in_close.wait(3)                              # ...and is stuck inside
        assert rig.session.manual is None                    # the controller already taken

        rig.session.reset_drone()
        time.sleep(0.3)                                      # the reset has had every chance
        release.set()
        rig.session.wait_idle()
        time.sleep(0.2)
        assert rig.session.snapshot().state is State.AWAITING_CONFIRMATION

    def test_with_no_session_it_is_a_plain_restart(self, tmp_path, monkeypatch):
        rig = self.make(tmp_path, monkeypatch)
        sign_in(rig)
        rig.session.reset_drone()
        assert rig.cycled == ["radio://0/80/2M"]
        assert rig.session.snapshot().radio["state"] == "restarting"
        assert rig.session.snapshot().state is State.IDLE

    def test_a_drone_that_does_not_come_back_says_so(self, tmp_path, monkeypatch):
        rig = self.make(tmp_path, monkeypatch)
        self.flying(rig)
        rig.session._reset_reconnect_s = 0.05
        real_cycle = rig.cycled.append

        def cycle_and_vanish(uri):
            real_cycle(uri)
            rig.link.open_error = LinkError("No Crazyflie found on the radio.")

        rig.session._power_cycle = cycle_and_vanish
        rig.session.reset_drone()
        rig.session.wait_idle()
        snap = rig.session.snapshot()
        assert snap.state is State.CHECKS_FAILED
        assert snap.retry_required
        assert "did not answer" in snap.message
        rig.link.open_error = None                           # switched on by hand
        rig.session.retry()
        rig.session.wait_idle()
        assert rig.session.snapshot().state is State.AWAITING_CONFIRMATION

    def test_a_radio_that_cannot_restart_it_says_to_do_it_by_hand(self, tmp_path, monkeypatch):
        rig = self.make(tmp_path, monkeypatch)
        self.flying(rig)

        def refuse(uri):
            raise OSError("radio busy")

        rig.session._power_cycle = refuse
        rig.session.reset_drone()
        rig.session.wait_idle()
        snap = rig.session.snapshot()
        assert snap.state is State.CHECKS_FAILED
        assert "by hand" in snap.message

    def test_not_while_the_checks_are_running(self, tmp_path, monkeypatch):
        rig = self.make(tmp_path, monkeypatch)
        start_and_confirm(rig)
        rig.session._set(state=State.STARTING)
        with pytest.raises(SessionError, match="Wait"):
            rig.session.reset_drone()
        assert rig.cycled == []
