"""The session checks, driven by a fake clock and a scripted telemetry source."""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace

import pytest

from dronedeck.flight import checks, supervisor
from dronedeck.flight.checks import (
    BATTERY_TEST_WAIT_S,
    BatteryTestResult,
    CheckKey,
    ChecksFailed,
    CheckStatus,
    HealthTestResult,
    PropTestResult,
    battery_percent,
    collect,
    read_hardware_id,
    run_battery_test,
    run_checks,
    run_prop_test,
)
from dronedeck.telemetry.stream import Snapshot

# The lab drone's real MCU id, read 2026-09-16.
LAB_CPU_ID = {"cpu.id0": "3080234", "cpu.id1": "859064082", "cpu.id2": "959656757"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def healthy(clock: FakeClock, **overrides: float) -> Snapshot:
    values = {
        "pm.vbat": 4.05, "sys.canfly": 1,
        "supervisor.info": supervisor.CAN_BE_ARMED | supervisor.CAN_FLY,
        "lighthouse.bsReceive": 0b11, "lighthouse.bsCalVal": 0b11,
        "lighthouse.bsGeoVal": 0b11, "lighthouse.bsAvailable": 0b1111,
        "kalman.varPX": 0.0004, "kalman.varPY": 0.0004, "kalman.varPZ": 0.0004,
        "stateEstimate.x": 0.10, "stateEstimate.y": -0.20, "stateEstimate.z": 0.012,
    } | overrides
    return Snapshot(MappingProxyType(values), updated_at=clock.now)


def make_cf(**params: str):
    values = LAB_CPU_ID | {"deck.bcLighthouse4": "1"} | params
    writes: list[tuple[str, str]] = []
    return SimpleNamespace(param=SimpleNamespace(
        get_value=lambda name: values[name],
        set_value=lambda name, value: writes.append((name, value)),
        writes=writes,
    ))


def run(cf, source, clock, **kwargs):
    steps: list = []
    report = collect(run_checks(cf, source, clock=clock, sleep=clock.sleep, **kwargs), steps.append)
    return report, steps


class TestHappyPath:
    def test_every_check_passes_in_order_and_reports_ground(self):
        clock = FakeClock()
        resets: list[bool] = []
        report, steps = run(make_cf(), lambda: healthy(clock), clock,
                            reset_estimator=lambda: resets.append(True))

        passed = [s.key for s in steps if s.status is CheckStatus.PASSED]
        assert passed == list(CheckKey)
        assert all(steps[i].status is CheckStatus.RUNNING for i in range(0, len(steps), 2))
        assert report.hardware_id == "cf-002f002a3334471239333335"
        assert report.ground_z_m == pytest.approx(0.012)
        assert report.takeoff_xy == pytest.approx((0.10, -0.20))
        assert resets == [True]

    def test_battery_step_carries_display_values(self):
        clock = FakeClock()
        _, steps = run(make_cf(), lambda: healthy(clock), clock)
        battery = next(s for s in steps
                       if s.key is CheckKey.BATTERY and s.status is CheckStatus.PASSED)
        assert battery.data == {"vbat": 4.05, "percent": 83, "canfly": True, "endurance_s": 200}


class TestFailures:
    def failing_step(self, cf, source, clock):
        steps: list = []
        with pytest.raises(ChecksFailed) as err:
            collect(run_checks(cf, source, clock=clock, sleep=clock.sleep), steps.append)
        assert steps[-1].status is CheckStatus.FAILED
        assert steps[-1] is err.value.result
        return steps[-1]

    def warning_step(self, cf, source, clock, key: CheckKey):
        """Positioning problems warn and degrade; they no longer block."""
        steps: list = []
        report = collect(run_checks(cf, source, clock=clock, sleep=clock.sleep), steps.append)
        warned = next(s for s in steps if s.key is key and s.status is CheckStatus.WARNING)
        assert not report.assisted
        assert report.unassisted_reason
        return report, warned

    def test_the_lab_crash_conditions_leave_the_drone_unassisted(self):
        """The 2026-09-16 conditions: 4 stations stored, 0 received.

        The operator may now fly by hand anyway — that is their call — but the
        drone must never be told it can hold a position. `assisted` False is
        what stops a program running and what switches off the guards that
        would otherwise steer by the accelerometer integrating.
        """
        clock = FakeClock()
        crash = lambda: healthy(  # noqa: E731
            clock, **{"lighthouse.bsReceive": 0, "kalman.varPX": 4.38, "kalman.varPY": 4.38})
        report, warned = self.warning_step(make_cf(), crash, clock, CheckKey.POSITIONING)
        assert "No base station signal" in warned.detail
        assert warned.data["stored"] == [0, 1, 2, 3]
        assert warned.data["received"] == []
        assert report.positioning.usable == ()

    def test_base_stations_get_time_to_lock_on(self):
        clock = FakeClock()
        source = lambda: healthy(  # noqa: E731
            clock, **({"lighthouse.bsReceive": 0} if clock.now < 3 else {}))
        report, _ = run(make_cf(), source, clock)
        assert report.positioning.usable == (0, 1)

    def test_firmware_refuses_to_arm(self):
        clock = FakeClock()
        failed = self.failing_step(make_cf(), lambda: healthy(clock, **{"sys.canfly": 0}), clock)
        assert failed.key is CheckKey.BATTERY
        assert "sys.canfly=0" in failed.detail

    def test_no_deck_leaves_the_drone_unassisted(self):
        clock = FakeClock()
        report, warned = self.warning_step(
            make_cf(**{"deck.bcLighthouse4": "0"}), lambda: healthy(clock), clock, CheckKey.DECK)
        assert "unassisted" in warned.detail
        assert not report.assisted

    def test_no_deck_is_the_reason_given_not_the_base_stations(self):
        """The lab, 2026-09-30: deck.bcLighthouse4 = 0 and nothing received.
        For a week the reason said "check the base stations are on" while they
        sat solid green; the deck was the fault. It must be named alone."""
        clock = FakeClock()
        lab = lambda: healthy(clock, **{"lighthouse.bsReceive": 0,  # noqa: E731
                                        "lighthouse.bsAvailable": 0})
        report, warned = self.warning_step(
            make_cf(**{"deck.bcLighthouse4": "0"}), lab, clock, CheckKey.DECK)
        assert report.unassisted_reason == checks.NO_LIGHTHOUSE_DECK
        assert warned.detail == checks.NO_LIGHTHOUSE_DECK
        assert "No base station signal" not in report.unassisted_reason
        assert not report.lighthouse_deck

    def test_the_battery_waits_for_its_own_log_block(self):
        """The first sample can hold one log block and not the one carrying the
        battery. Measured 2026-09-30: `check` failed "Battery readings are not
        available" against a drone reporting 3.80 V."""
        clock = FakeClock()

        def source() -> Snapshot:
            if clock.now < 0.3:
                partial = {"supervisor.info": supervisor.CAN_BE_ARMED | supervisor.CAN_FLY}
                return Snapshot(MappingProxyType(partial), updated_at=clock.now)
            return healthy(clock)

        report, steps = run(make_cf(), source, clock)
        battery = next(s for s in steps
                       if s.key is CheckKey.BATTERY and s.status is not CheckStatus.RUNNING)
        assert battery.status is CheckStatus.PASSED
        assert report.vbat == pytest.approx(4.05)

    def test_the_battery_still_fails_when_it_never_arrives(self):
        clock = FakeClock()
        no_battery = lambda: Snapshot(MappingProxyType(  # noqa: E731
            {"supervisor.info": supervisor.CAN_BE_ARMED | supervisor.CAN_FLY}),
            updated_at=clock.now)
        failed = self.failing_step(make_cf(), no_battery, clock)
        assert failed.key is CheckKey.BATTERY
        assert "not available" in failed.detail

    def test_no_telemetry(self):
        clock = FakeClock()
        stale = lambda: Snapshot(MappingProxyType({}), updated_at=None)  # noqa: E731
        failed = self.failing_step(make_cf(), stale, clock)
        assert failed.key is CheckKey.TELEMETRY

    def test_z_settled_but_x_wandering_is_not_settled(self):
        # The exact hole: the old preflight looked at z only. It no longer
        # blocks, but a wandering estimate must never count as assisted.
        clock = FakeClock()
        wander = lambda: healthy(clock, **{"stateEstimate.x": clock.now * 0.5})  # noqa: E731
        report, warned = self.warning_step(make_cf(), wander, clock, CheckKey.ESTIMATE)
        assert "never settled" in warned.detail
        assert not report.assisted

    def test_unreadable_identity(self):
        clock = FakeClock()
        cf = make_cf()
        cf.param.get_value = lambda name: (_ for _ in ()).throw(KeyError(name))
        failed = self.failing_step(cf, lambda: healthy(clock), clock)
        assert failed.key is CheckKey.IDENTITY


class TestHelpers:
    def test_hardware_id_is_stable_and_distinct_from_the_radio_address(self):
        cf = make_cf()
        assert read_hardware_id(cf) == read_hardware_id(cf)
        assert "radio" not in read_hardware_id(cf)

    @pytest.mark.parametrize(("vbat", "percent"),
                             [(4.2, 100), (4.4, 100), (3.75, 50), (3.3, 0), (3.0, 0)])
    def test_battery_percent_is_clamped(self, vbat, percent):
        assert battery_percent(vbat) == percent


class TestPropTest:
    def test_reads_each_motor(self):
        clock = FakeClock()
        cf = make_cf()
        readings = iter([(4, 0), (4, 0), (5, 0b1011)])
        result = run_prop_test(cf, lambda: next(readings), clock=clock, sleep=clock.sleep)
        assert cf.param.writes == [("health.startPropTest", "1")]
        assert result.passed == (1, 2, 4)
        assert result.failed == (3,)
        assert not result.ok

    def test_all_four_pass(self):
        clock = FakeClock()
        readings = iter([(0, 0), (1, 0b1111)])
        assert run_prop_test(make_cf(), lambda: next(readings), clock=clock, sleep=clock.sleep).ok

    def test_times_out_without_a_result(self):
        clock = FakeClock()
        with pytest.raises(TimeoutError):
            run_prop_test(make_cf(), lambda: (7, 0), clock=clock, sleep=clock.sleep)


class TestMotors:
    """The supervisor holding the motors after a crash (flight/supervisor.py)."""

    def test_a_crashed_drone_is_recovered_and_the_checks_carry_on(self):
        clock = FakeClock()
        bits = {"value": supervisor.IS_CRASHED | supervisor.CAN_BE_ARMED}
        requests: list[bool] = []

        def recover():
            requests.append(True)
            bits["value"] = supervisor.CAN_BE_ARMED | supervisor.CAN_FLY

        report, steps = run(make_cf(), lambda: healthy(clock, **{"supervisor.info": bits["value"]}),
                            clock, request_recovery=recover)
        motors = next(s for s in steps
                      if s.key is CheckKey.MOTORS and s.status is not CheckStatus.RUNNING)
        assert requests == [True]
        assert motors.status is CheckStatus.PASSED
        assert "recovered" in motors.detail
        assert report.hardware_id

    def test_a_locked_drone_fails_with_restart_advice_and_no_recovery_attempt(self):
        clock = FakeClock()
        requests: list[bool] = []
        with pytest.raises(ChecksFailed) as err:
            run(make_cf(), lambda: healthy(clock, **{"supervisor.info": supervisor.IS_LOCKED}),
                clock, request_recovery=lambda: requests.append(True))
        assert err.value.result.key is CheckKey.MOTORS
        assert "restart" in err.value.result.detail
        assert requests == []

    def test_a_drone_that_stays_tumbled_says_stand_it_up(self):
        clock = FakeClock()
        with pytest.raises(ChecksFailed) as err:
            run(make_cf(), lambda: healthy(clock, **{"supervisor.info": supervisor.IS_TUMBLED}),
                clock, request_recovery=lambda: None)
        assert "not upright" in err.value.result.detail

    def test_a_crash_that_does_not_clear_fails_after_the_timeout(self):
        clock = FakeClock()
        with pytest.raises(ChecksFailed) as err:
            run(make_cf(), lambda: healthy(clock, **{"supervisor.info": supervisor.IS_CRASHED}),
                clock, request_recovery=lambda: None)
        assert "off and on" in err.value.result.detail
        assert clock.now >= supervisor.RECOVERY_TIMEOUT_S

    def test_firmware_without_the_supervisor_variable_is_not_blocked(self):
        clock = FakeClock()

        def source():
            snap = healthy(clock)
            values = {k: v for k, v in snap.values.items() if k != "supervisor.info"}
            return Snapshot(MappingProxyType(values), updated_at=clock.now)

        _, steps = run(make_cf(), source, clock)
        motors = next(s for s in steps
                      if s.key is CheckKey.MOTORS and s.status is not CheckStatus.RUNNING)
        assert motors.status is CheckStatus.WARNING


class TestBatteryTest:
    def test_runs_the_firmware_test_and_reports_its_own_verdict(self):
        clock = FakeClock()
        cf = make_cf(**{"health.batTestPWMRatio": "0.0"})
        result = run_battery_test(cf, lambda: (0.18, 1), lambda: healthy(clock), sleep=clock.sleep)
        assert ("health.startBatTest", "1") in cf.param.writes
        assert clock.now >= BATTERY_TEST_WAIT_S                  # waited for it to run
        assert result == BatteryTestResult(sag_v=0.18, passed=True, idle_vbat=4.05, pwm_ratio=0.0)

    def test_a_failing_battery_is_the_firmware_saying_so(self):
        clock = FakeClock()
        result = run_battery_test(make_cf(**{"health.batTestPWMRatio": "0.0"}),
                                  lambda: (0.72, 0), lambda: healthy(clock), sleep=clock.sleep)
        assert not result.passed and result.sag_v == pytest.approx(0.72)

    def test_an_unreadable_ratio_is_recorded_as_unknown_not_invented(self):
        clock = FakeClock()
        result = run_battery_test(make_cf(), lambda: (0.2, 1), lambda: healthy(clock),
                                  sleep=clock.sleep)
        assert result.pwm_ratio is None

    def test_the_health_test_is_ok_only_when_both_halves_pass(self):
        motors_ok = PropTestResult((1, 2, 3, 4), ())
        battery_ok = BatteryTestResult(0.2, True, 4.1, 0.0)
        assert HealthTestResult(motors_ok, battery_ok).ok
        assert not HealthTestResult(PropTestResult((1, 2, 4), (3,)), battery_ok).ok
        assert not HealthTestResult(motors_ok, BatteryTestResult(0.7, False, 4.1, 0.0)).ok
        assert not HealthTestResult(motors_ok, None, "did not report").ok
        assert HealthTestResult(motors_ok, battery_ok).to_dict()["battery"]["sag_v"] == 0.2



class TestWhyItWillNotArm:
    """sys.canfly = 0 is a verdict, not a reason.

    Reading it as "flat battery" sent an operator to the charger on
    2026-09-22 holding a cell at 4.15 V, while supervisor.info had the real
    answer — a latched crash — the whole time.
    """

    def test_a_latched_crash_is_not_reported_as_a_flat_battery(self):
        reason = checks.refusal_reason(4.15, 0, supervisor.IS_CRASHED)
        assert "crash" in reason.lower()
        assert "charge" not in reason.lower()

    def test_being_upside_down_says_so(self):
        reason = checks.refusal_reason(4.15, 0, supervisor.IS_TUMBLED)
        assert "upright" in reason

    def test_locked_motors_say_so(self):
        reason = checks.refusal_reason(4.15, 0, supervisor.IS_LOCKED)
        assert "locked" in reason.lower()

    def test_the_battery_is_blamed_only_when_the_firmware_blames_it(self):
        reason = checks.refusal_reason(3.15, 3, 0)      # pm.state 3 = low power
        assert "Charge the battery" in reason
        assert "3.15 V" in reason

    def test_an_unexplained_refusal_hands_over_the_numbers(self):
        """Worse than a wrong reason is a confident wrong reason. With nothing
        to go on it says so and prints what it read."""
        reason = checks.refusal_reason(4.15, 0, 0)
        assert "has not said why" in reason
        assert "4.15 V" in reason and "supervisor.info=0" in reason
        assert "charging it is" in reason           # explicitly steers away


class TestStateLeftFromTheLastFlight:
    """IS_FLYING set while the drone is on the floor is last flight's state.

    Measured on six of nine flights on 2026-09-22, all following a flight that
    ended abnormally. Nothing cflib offers on this firmware clears it — disarm,
    re-arm, a stop setpoint and a same-value controller write were all tried on
    the drone and supervisor.info did not move. So it is reported, not fixed.
    """

    def test_a_drone_that_thinks_it_is_still_flying_is_flagged(self):
        check = supervisor.ensure_motors_unlocked(
            lambda: supervisor.IS_ARMED | supervisor.CAN_FLY | supervisor.IS_FLYING, None)
        assert check.state is supervisor.MotorState.STILL_FLYING
        assert check.ok                      # it still flies; it is not a refusal
        assert "switch it off and on" in check.message.lower()

    def test_a_settled_drone_is_simply_ready(self):
        check = supervisor.ensure_motors_unlocked(
            lambda: supervisor.IS_ARMED | supervisor.CAN_FLY, None)
        assert check.state is supervisor.MotorState.READY

    def test_the_bits_are_named_not_just_totalled(self):
        check = supervisor.ensure_motors_unlocked(
            lambda: supervisor.IS_ARMED | supervisor.CAN_FLY | supervisor.IS_FLYING, None)
        assert check.names == ("is_armed", "can_fly", "is_flying")

    def test_a_firmware_that_says_nothing_names_nothing(self):
        assert supervisor.ensure_motors_unlocked(lambda: None, None).names == ()
