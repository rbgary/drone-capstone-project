# This file was modified on 11/4/2026 with the assistance of Claude Code in VS Code
# (Claude Opus 5.5, Anthropic, 2026). Replaces the test that an unmeasured received
# station always blocks with three: it does not block a settled position, the 5 cm check
# still applies, and a room requiring two stations is still told to measure the second.
# Adds tests for uncertainty_cm()'s round-up.

"""Positioning readiness and the in-flight guard.

Several cases replay the lab crash of 2026-09-16 from real readings: the
at-rest capture (no stations received, 4 stored, variance ~4.4 m²) and the
hover trace whose height estimate fell below the floor while the loop held.
"""

from __future__ import annotations

from types import MappingProxyType

from dronedeck.safety.flight_guard import (
    CRITICAL_VBAT,
    MAX_DRIFT_M,
    PM_LOW_POWER,
    SUPERVISOR_TUMBLED_BIT,
    Action,
    FlightGuard,
    GuardContext,
    Reason,
    assess_positioning,
    station_ids,
    uncertainty_cm,
)
from dronedeck.telemetry.stream import Snapshot

GROUND = 1.0


def snap(now: float = 10.0, **values: float) -> Snapshot:
    """A healthy hovering sample at 0.3 m, overridable per test."""
    base = {
        "stateEstimate.x": 0.0, "stateEstimate.y": 0.0, "stateEstimate.z": GROUND + 0.3,
        "stateEstimate.vx": 0.0, "stateEstimate.vy": 0.0, "stateEstimate.vz": 0.0,
        "kalman.varPX": 0.0004, "kalman.varPY": 0.0004, "kalman.varPZ": 0.0004,
        "lighthouse.bsReceive": 0b11, "lighthouse.bsCalVal": 0b11,
        "lighthouse.bsGeoVal": 0b11, "lighthouse.bsAvailable": 0b11,
        "pm.vbat": 3.9, "pm.state": 0, "supervisor.info": 0,
    }
    renamed = {k.replace("__", "."): v for k, v in values.items()}
    return Snapshot(MappingProxyType(base | renamed), updated_at=now)


def hold_guard() -> FlightGuard:
    """A hold program at 0.3 m whose climb has completed."""
    guard = FlightGuard(GuardContext(
        ground_z=GROUND, fence_half_extent_m=2.0, max_height_m=1.0,
        takeoff_xy=(0.0, 0.0), target_height_m=0.3,
    ))
    guard.arm_hold_checks(now=0.0)
    return guard


class TestStationIds:
    def test_bits_to_ids(self):
        assert station_ids(0b1011) == (0, 1, 3)

    def test_missing(self):
        assert station_ids(None) == ()


class TestUncertaintyCm:
    def test_rounds_up_so_a_refused_reading_never_shows_as_five(self):
        assert uncertainty_cm(0.00254) == 5.1             # 5.04 cm, refused
        assert uncertainty_cm(0.0025) == 5.0              # exactly the limit, allowed

    def test_float_noise_is_not_rounded_up(self):
        assert uncertainty_cm(0.0004) == 2.0              # not 2.1
        assert uncertainty_cm(0.0001) == 1.0


class TestPositioningReadiness:
    def test_the_lab_capture_that_preceded_the_crash_is_not_ready(self):
        status = assess_positioning(snap(**{
            "lighthouse__bsReceive": 0, "lighthouse__bsAvailable": 0b1111,
            "lighthouse__bsCalVal": 0b11, "lighthouse__bsGeoVal": 0b11,
            "kalman__varPX": 4.38, "kalman__varPY": 4.38, "kalman__varPZ": 0.018,
        }))
        assert not status.ready
        assert status.stored == (0, 1, 2, 3)
        assert status.received == ()
        problems = " ".join(status.problems())
        assert "No base station signal" in problems
        assert "uncertain" in problems

    def test_two_received_valid_stations_and_low_variance_is_ready(self):
        assert assess_positioning(snap()).ready

    def test_an_unmeasured_extra_station_does_not_block_a_settled_position(self):
        """2026-11-04: Ch 1 measured and settled under 5 cm, Ch 2 in view but
        not yet measured. The drone's estimator ignores a station it has no
        place for, so the 5 cm check decides — Continue is not held back."""
        status = assess_positioning(snap(**{
            "lighthouse__bsReceive": 0b11, "lighthouse__bsCalVal": 0b11,
            "lighthouse__bsGeoVal": 0b01,
        }))
        assert status.usable == (0,)
        assert status.received_without_geometry == (1,)
        assert status.ready
        assert status.problems() == []

    def test_an_unmeasured_extra_station_still_needs_a_settled_position(self):
        status = assess_positioning(snap(**{
            "lighthouse__bsReceive": 0b11, "lighthouse__bsCalVal": 0b11,
            "lighthouse__bsGeoVal": 0b01,
            "kalman__varPX": 0.0036, "kalman__varPY": 0.0004, "kalman__varPZ": 0.0004,
        }))
        assert not status.ready
        assert "6.0 cm" in " ".join(status.problems())

    def test_a_room_that_insists_on_two_is_told_to_measure_the_second(self, monkeypatch):
        monkeypatch.setenv("DRONEDECK_MIN_STATIONS", "2")
        status = assess_positioning(snap(**{
            "lighthouse__bsReceive": 0b11, "lighthouse__bsCalVal": 0b11,
            "lighthouse__bsGeoVal": 0b01,
        }))
        assert not status.ready
        assert status.received_without_geometry == (1,)
        said = " ".join(status.problems())
        # Names the app step that fixes it, not a terminal command.
        assert "② Position" in said
        # 2026-10-05: one cause, said once — not also "wait for the
        # calibration" (already read) or "blocked or off" (it was received).
        assert "calibration" not in said and "blocked or off" not in said

    def test_a_station_still_sending_its_calibration_is_waited_for(self):
        status = assess_positioning(snap(**{
            "lighthouse__bsReceive": 0b1, "lighthouse__bsCalVal": 0b0,
        }))
        said = " ".join(status.problems())
        assert "calibration has not arrived" in said
        assert "② Position" not in said

    def test_one_station_is_enough_to_fly(self):
        """Lighthouse V2 resolves a pose from a single unit's two sweeps.
        Measured in the cage 2026-09-22: one station, drone at rest, position
        held to sub-millimetre."""
        status = assess_positioning(snap(**{"lighthouse__bsReceive": 0b01}))
        assert status.ready
        assert status.problems() == []

    def test_a_room_with_two_can_insist_on_both(self, monkeypatch):
        """A second station is a second viewpoint. Where they exist, wanting
        both is worth asking for — the pair solver needs two, and only two
        give an occluded deck somewhere else to look."""
        monkeypatch.setenv("DRONEDECK_MIN_STATIONS", "2")
        status = assess_positioning(snap(**{"lighthouse__bsReceive": 0b01}))
        assert not status.ready
        assert "Only 1 usable" in " ".join(status.problems())

    def test_no_station_at_all_is_still_refused(self):
        """Zero is not a looser threshold, it is a different control law.
        With no station there is no position for anything to be held against."""
        status = assess_positioning(snap(**{"lighthouse__bsReceive": 0b00}))
        assert not status.ready
        assert "No base station signal" in " ".join(status.problems())

    def test_asking_for_none_still_requires_one(self, monkeypatch):
        monkeypatch.setenv("DRONEDECK_MIN_STATIONS", "0")
        status = assess_positioning(snap(**{"lighthouse__bsReceive": 0b00}))
        assert not status.ready

    def test_missing_variance_is_not_ready(self):
        s = snap()
        values = dict(s.values)
        del values["kalman.varPZ"]
        assert not assess_positioning(Snapshot(MappingProxyType(values), s.updated_at)).ready


class TestGuardPositionLost:
    def test_the_crash_trace_stops_the_motors(self):
        # The hover loop printed -1.53 m AGL and kept holding.
        verdict = hold_guard().check(snap(**{"stateEstimate__z": GROUND - 1.53}), now=10.0)
        assert verdict.action is Action.STOP
        assert verdict.reason is Reason.POSITION_LOST

    def test_implausible_speed(self):
        verdict = hold_guard().check(snap(**{"stateEstimate__vx": 3.0}), now=10.0)
        assert verdict.action is Action.STOP

    def test_huge_variance(self):
        verdict = hold_guard().check(snap(**{"kalman__varPY": 4.4}), now=10.0)
        assert verdict.action is Action.STOP

    def test_far_above_ceiling(self):
        verdict = hold_guard().check(snap(**{"stateEstimate__z": GROUND + 3.66}), now=10.0)
        assert verdict.action is Action.STOP

    def test_tumbled_flag(self):
        verdict = hold_guard().check(
            snap(**{"supervisor__info": 1 << SUPERVISOR_TUMBLED_BIT}), now=10.0
        )
        assert verdict.reason is Reason.TUMBLED


class TestGuardLands:
    def test_healthy_hover_is_ok(self):
        assert hold_guard().check(snap(), now=10.0).ok

    def test_a_brief_blackout_is_ridden_out_while_the_estimate_holds(self):
        """Measured 2026-09-22: a 0.5 s dropout landed a drone holding x and y
        to 1.7 cm, and the beams were back 0.2 s later. With one base station
        a brief occlusion is ordinary — the airframe's own tilt can cause it."""
        guard = hold_guard()
        lost = {"lighthouse__bsReceive": 0}
        for t in (10.0, 10.4, 10.6, 11.0, 12.0, 12.9):
            assert guard.check(snap(now=t, **lost), now=t).ok, f"landed at {t}"

    def test_a_long_blackout_lands_however_good_it_looks(self):
        guard = hold_guard()
        lost = {"lighthouse__bsReceive": 0}
        guard.check(snap(now=10.0, **lost), now=10.0)
        verdict = guard.check(snap(now=13.1, **lost), now=13.1)
        assert verdict.reason is Reason.RECEPTION_LOST
        assert verdict.action is Action.LAND

    def test_a_blackout_lands_fast_when_the_estimate_is_going_with_it(self):
        """The filter's variance grows when it has nothing to correct with.
        That, not the beams, is what says the position is being lost."""
        guard = hold_guard()
        lost = {"lighthouse__bsReceive": 0,
                "kalman__varPX": 0.10, "kalman__varPY": 0.10}   # 32 cm sd
        assert guard.check(snap(now=10.0, **lost), now=10.0).ok
        verdict = guard.check(snap(now=10.6, **lost), now=10.6)
        assert verdict.reason is Reason.RECEPTION_LOST
        assert verdict.action is Action.LAND
        assert "estimate is going with it" in verdict.message

    def test_reception_returning_resets_the_grace_period(self):
        guard = hold_guard()
        guard.check(snap(now=10.0, **{"lighthouse__bsReceive": 0}), now=10.0)
        guard.check(snap(now=10.3), now=10.3)
        assert guard.check(snap(now=10.6, **{"lighthouse__bsReceive": 0}), now=10.6).ok

    def test_grace_period_starting_at_time_zero(self):
        guard = hold_guard()
        degrading = {"lighthouse__bsReceive": 0,
                     "kalman__varPX": 0.10, "kalman__varPY": 0.10}
        guard.check(snap(now=0.0, **degrading), now=0.0)
        assert not guard.check(snap(now=0.6, **degrading), now=0.6).ok

    def test_firmware_low_power_state_ends_the_operation(self):
        verdict = hold_guard().check(snap(**{"pm__state": PM_LOW_POWER}), now=10.0)
        assert verdict.action is Action.LAND
        assert verdict.reason is Reason.BATTERY_LOW

    def test_a_momentary_voltage_sag_is_not_empty(self):
        guard = hold_guard()
        low = {"pm__vbat": CRITICAL_VBAT - 0.05}
        assert guard.check(snap(now=10.0, **low), now=10.0).ok
        assert guard.check(snap(now=10.5), now=10.5).ok
        assert guard.check(snap(now=11.2, **low), now=11.2).ok

    def test_sustained_low_voltage_lands(self):
        guard = hold_guard()
        low = {"pm__vbat": CRITICAL_VBAT - 0.05}
        guard.check(snap(now=10.0, **low), now=10.0)
        assert guard.check(snap(now=11.1, **low), now=11.1).reason is Reason.BATTERY_LOW

    def test_drift_from_takeoff_point(self):
        verdict = hold_guard().check(snap(**{"stateEstimate__x": MAX_DRIFT_M + 0.05}), now=10.0)
        assert verdict.reason is Reason.DRIFT

    def test_drift_is_not_checked_before_the_climb_completes(self):
        guard = FlightGuard(GuardContext(
            ground_z=GROUND, fence_half_extent_m=2.0, max_height_m=1.0,
            takeoff_xy=(0.0, 0.0), target_height_m=0.3,
        ))
        assert guard.check(snap(**{"stateEstimate__x": 0.4}), now=10.0).ok

    def test_height_error_must_persist(self):
        guard = hold_guard()
        low = {"stateEstimate__z": GROUND + 0.02}
        assert guard.check(snap(now=10.0, **low), now=10.0).ok
        assert guard.check(snap(now=11.1, **low), now=11.1).reason is Reason.HEIGHT_ERROR

    def test_outside_fence(self):
        verdict = hold_guard().check(snap(**{"stateEstimate__y": -2.1}), now=10.0)
        assert verdict.reason is Reason.OUTSIDE_FENCE

    def test_a_brief_telemetry_gap_is_ridden_out(self):
        """Losing the DOWNLINK means we are blind, not that the drone is in
        danger: setpoints still go up at 50 Hz. One late sample used to land a
        drone that was otherwise flying well (2026-09-22)."""
        assert hold_guard().check(snap(now=10.0), now=10.6).ok
        assert hold_guard().check(snap(now=10.0), now=11.9).ok

    def test_a_long_telemetry_silence_lands(self):
        verdict = hold_guard().check(snap(now=10.0), now=12.1)
        assert verdict.reason is Reason.TELEMETRY_STALE
        assert verdict.action is Action.LAND

    def test_while_blind_no_other_guard_speaks(self):
        """A stale snapshot cannot say anything true about now, so reading a
        tumble or a position out of it would be inventing one."""
        tumbled = {"supervisor__info": 1 << 5}
        assert hold_guard().check(snap(now=10.0, **tumbled), now=10.9).ok


class TestManualFlight:
    def manual_guard(self) -> FlightGuard:
        return FlightGuard(GuardContext(ground_z=GROUND, fence_half_extent_m=2.0, max_height_m=1.0))

    def test_moving_on_purpose_is_not_drift(self):
        assert self.manual_guard().check(snap(**{"stateEstimate__x": 1.2}), now=10.0).ok

    def test_battery_still_ends_manual_flight(self):
        verdict = self.manual_guard().check(snap(**{"pm__state": PM_LOW_POWER}), now=10.0)
        assert verdict.reason is Reason.BATTERY_LOW

    def test_fence_still_applies(self):
        verdict = self.manual_guard().check(snap(**{"stateEstimate__x": 2.5}), now=10.0)
        assert verdict.reason is Reason.OUTSIDE_FENCE


class TestUnassistedGuards:
    """With no position estimate, the position-derived guards must be OFF.

    Left on, they read the accelerometer integrating: the drift, fence and
    plausibility checks all fire within seconds, and a "landing" would steer by
    a number that means nothing. What must keep working is everything that does
    not need a position — tumble, battery, and stale telemetry.
    """

    @staticmethod
    def guard() -> FlightGuard:
        return FlightGuard(GuardContext(
            ground_z=GROUND, fence_half_extent_m=2.0, max_height_m=1.0,
            takeoff_xy=(0.0, 0.0), assisted=False,
        ))

    def test_a_drifting_estimate_does_not_abort(self):
        guard = self.guard()
        guard.arm_hold_checks(10.0)
        wild = snap(
            stateEstimate__x=50.0, stateEstimate__y=-50.0, stateEstimate__z=-30.0,
            stateEstimate__vx=9.0, kalman__varPX=4.38, kalman__varPY=4.38,
            **{"lighthouse.bsReceive": 0},
        )
        assert guard.check(wild, 10.0).action is Action.OK

    def test_no_position_estimate_at_all_does_not_abort(self):
        guard = self.guard()
        bare = Snapshot(MappingProxyType({"pm.vbat": 3.9, "pm.state": 0}), updated_at=10.0)
        assert guard.check(bare, 10.0).action is Action.OK

    def test_a_tumble_still_stops_the_motors(self):
        verdict = self.guard().check(
            snap(supervisor__info=1 << SUPERVISOR_TUMBLED_BIT), 10.0)
        assert verdict.action is Action.STOP and verdict.reason is Reason.TUMBLED

    def test_low_battery_still_ends_the_flight(self):
        verdict = self.guard().check(snap(pm__state=PM_LOW_POWER), 10.0)
        assert verdict.action is Action.LAND and verdict.reason is Reason.BATTERY_LOW

    def test_a_critical_voltage_still_lands_after_the_grace_period(self):
        guard = self.guard()
        assert guard.check(snap(pm__vbat=CRITICAL_VBAT - 0.1), 10.0).action is Action.OK
        verdict = guard.check(snap(now=12.0, pm__vbat=CRITICAL_VBAT - 0.1), 12.0)
        assert verdict.action is Action.LAND and verdict.reason is Reason.BATTERY_LOW

    def test_stale_telemetry_still_lands(self):
        """Nine seconds old lands immediately — the age is the evidence, so
        there is no grace period left to start."""
        verdict = self.guard().check(snap(now=1.0), 10.0)
        assert verdict.action is Action.LAND and verdict.reason is Reason.TELEMETRY_STALE


class TestAssistedStillGuards:
    def test_lost_position_still_outranks_a_low_battery(self):
        """Ordering matters: a landing steers by the estimate, so when the
        estimate is gone the motors stop instead."""
        guard = FlightGuard(GuardContext(
            ground_z=GROUND, fence_half_extent_m=2.0, max_height_m=1.0))
        verdict = guard.check(
            snap(pm__state=PM_LOW_POWER, kalman__varPX=4.38, kalman__varPY=4.38), 10.0)
        assert verdict.action is Action.STOP and verdict.reason is Reason.POSITION_LOST


class TestCrashCutoff:
    """The guard reads the attitude itself, because the firmware's tumble flag
    came 24 s late on 2026-09-30 (trace_079af354): after the flip the drone sat
    at pitch -38 deg with thrust pinned at 65535 until the operator pressed
    Emergency stop. Values below are that trace's own rows."""

    @staticmethod
    def guard(assisted: bool = False) -> FlightGuard:
        return FlightGuard(GuardContext(
            ground_z=GROUND, fence_half_extent_m=2.0, max_height_m=1.0, assisted=assisted,
        ))

    def test_upside_down_stops_on_one_sample(self):
        # 48:19.42 — roll 157.9, pitch 2.9: the flip itself.
        verdict = self.guard().check(
            snap(stabilizer__roll=157.9, stabilizer__pitch=2.9), now=10.0)
        assert verdict.action is Action.STOP and verdict.reason is Reason.TUMBLED
        assert "158°" in verdict.message

    def test_a_steep_lean_stops_once_it_has_lasted(self):
        # 48:20.63 onwards — roll 68.0, pitch -36.3: ~76 deg, held.
        guard = self.guard()
        steep = {"stabilizer__roll": 68.0, "stabilizer__pitch": -36.3}
        assert guard.check(snap(now=10.0, **steep), now=10.0).ok
        assert guard.check(snap(now=10.2, **steep), now=10.2).ok
        verdict = guard.check(snap(now=10.3, **steep), now=10.3)
        assert verdict.action is Action.STOP and verdict.reason is Reason.TUMBLED

    def test_a_lean_that_recovers_starts_the_clock_again(self):
        guard = self.guard()
        assert guard.check(snap(now=10.0, stabilizer__roll=65.0), now=10.0).ok
        assert guard.check(snap(now=10.2, stabilizer__roll=20.0), now=10.2).ok
        assert guard.check(snap(now=10.3, stabilizer__roll=65.0), now=10.3).ok
        assert guard.check(snap(now=10.5, stabilizer__roll=65.0), now=10.5).ok

    def test_the_steepest_normal_flight_is_left_alone(self):
        # 48.4 deg is the most any of 76 traces leaned before a crash began.
        guard = self.guard()
        for t in (10.0, 11.0, 12.0):
            assert guard.check(snap(now=t, stabilizer__roll=40.0, stabilizer__pitch=26.0),
                               now=t).ok

    def test_a_diagonal_lean_counts_in_full(self):
        # 45 deg of roll and 45 of pitch is 60 deg from upright, not 45.
        guard = self.guard()
        lean = {"stabilizer__roll": 46.0, "stabilizer__pitch": 46.0}
        guard.check(snap(now=10.0, **lean), now=10.0)
        assert guard.check(snap(now=10.3, **lean), now=10.3).reason is Reason.TUMBLED

    def test_it_runs_with_base_stations_too(self):
        verdict = self.guard(assisted=True).check(
            snap(stabilizer__roll=-178.0, stabilizer__pitch=26.6), now=10.0)
        assert verdict.reason is Reason.TUMBLED

    def test_full_power_for_two_seconds_lands(self):
        guard = self.guard()
        for t in (10.0, 11.0, 11.9):
            assert guard.check(snap(now=t, stabilizer__thrust=65535), now=t).ok
        verdict = guard.check(snap(now=12.0, stabilizer__thrust=65535), now=12.0)
        assert verdict.action is Action.LAND and verdict.reason is Reason.THRUST_CEILING

    def test_a_short_burst_at_full_power_is_ordinary(self):
        guard = self.guard()
        assert guard.check(snap(now=10.0, stabilizer__thrust=65535), now=10.0).ok
        assert guard.check(snap(now=11.4, stabilizer__thrust=65535), now=11.4).ok
        assert guard.check(snap(now=11.5, stabilizer__thrust=53000), now=11.5).ok
        assert guard.check(snap(now=12.0, stabilizer__thrust=65535), now=12.0).ok
        assert guard.check(snap(now=13.5, stabilizer__thrust=65535), now=13.5).ok
