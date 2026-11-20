# This file was modified on 11/20/2026 with the assistance of Claude Code in VS Code
# (Claude Opus 5.5, Anthropic, 2026). Graceful stops now end stop -> notify_stop (the
# hand-back that keeps the drone from locking after a landing); new tests pin that,
# and that the emergency stop still latches.

"""Manual control loop.

Every test here is about something going wrong or being done gently, because
manual control is the one surface where a software mistake drops a real
aircraft. A fake clock drives the timing so heartbeat expiry and the climb rate
can be tested in microseconds rather than by sleeping.
"""

from __future__ import annotations

import math

import pytest

from dronedeck.flight.keyframe import (
    FAR_OPERATOR_M,
    NEAR_OPERATOR_M,
    ActiveFrame,
    KeyFrame,
    Reason,
)
from dronedeck.flight.manual import (
    CLIMB_ACCEL_M_S2,
    CLIMB_RATE_M_S,
    HEARTBEAT_TIMEOUT_S,
    IDLE_THRUST,
    KEY_MOVE_SPEED_M_S,
    LAND_RATE_M_S,
    LAND_S,
    LEASH_M,
    MAX_HEIGHT_M,
    MAX_TILT_DEG,
    MAX_UNASSISTED_HEIGHT_M,
    MOVE_ACCEL_M_S2,
    MOVE_SPEED_M_S,
    TICK_S,
    TILT_RATE_DEG_S,
    TOUCHDOWN_HOLD_S,
    YAW_RATE_DEG_S,
    ControlState,
    Fix,
    Intent,
    ManualController,
)
from tests.fakes import FakeClock, FakeCommander

GROUND = 1.0


def glide_distance(seconds: float) -> float:
    """How far the height target moves from rest while a key is held."""
    ramp = CLIMB_RATE_M_S / CLIMB_ACCEL_M_S2
    if seconds <= ramp:
        return 0.5 * CLIMB_ACCEL_M_S2 * seconds**2
    return 0.5 * CLIMB_ACCEL_M_S2 * ramp**2 + CLIMB_RATE_M_S * (seconds - ramp)


#: Distance covered easing from full climb speed to a stop.
STOPPING_DISTANCE = CLIMB_RATE_M_S**2 / (2 * CLIMB_ACCEL_M_S2)


#: An arbitrary spot the estimator reports the drone at, so tests that care
#: about the commanded point have a number to recognise. Heading 0, so the
#: nose happens to point +x; that the arrows ignore the nose is pinned
#: separately, at 90, 180 and -116 degrees (TestArrowsIgnoreTheNose).
SOMEWHERE = Fix(1.5, -0.5, 0.0)


class Rig:
    def __init__(self, assisted: bool = True, fix: Fix | None = SOMEWHERE, *,
                 key_frame: KeyFrame = KeyFrame.OPERATOR,
                 operator: tuple[float, float] | None = None,
                 yaw: float | None = None):
        self.clock = FakeClock()
        self.cmd = FakeCommander()
        self.lands: list[tuple[float, float]] = []
        #: What the estimator reports. Moving it simulates the drone drifting.
        self.fix = fix
        #: The heading the drone reports when there is no fix (the gyro).
        self.yaw = yaw
        #: Every change of what the arrows mean, as the app would hear it.
        self.frames: list = []
        self.ctl = ManualController(
            self.cmd, ground_z=GROUND, land=lambda z, d: self.lands.append((z, d)),
            assisted=assisted, clock=self.clock,
            position=lambda: self.fix,
            heading=lambda: self.fix.yaw_deg if self.fix is not None else self.yaw,
            key_frame=key_frame, operator_xy=operator,
            on_frame_change=self.frames.append,
        )

    def run(self, seconds: float, intent: Intent | None = None, heartbeat: bool = True):
        if intent is not None:
            self.ctl.set_intent(intent)
        for _ in range(round(seconds / TICK_S)):
            self.clock.advance(TICK_S)
            if heartbeat:
                self.ctl.heartbeat()
            self.ctl.tick()

    def drift(self, dx: float, dy: float, speed: float = 0.5):
        """Push the reported position by (dx, dy) at a believable speed.

        Moving it in one step would be an estimator jump, which the loop
        rejects on purpose — so a test about drift has to drift.
        """
        start = self.fix
        distance = (dx * dx + dy * dy) ** 0.5
        ticks = max(1, round(distance / speed / TICK_S))
        for i in range(1, ticks + 1):
            self.fix = Fix(start.x + dx * i / ticks, start.y + dy * i / ticks, start.yaw_deg)
            self.run(TICK_S)

    def fly_to(self, height: float):
        """Hold W until the target is near `height`, then release and let it
        glide to a stop — which lands it at `height` within a centimetre."""
        self.ctl.arm()
        self.ctl.set_intent(Intent(up=True))
        while self.ctl.target_height + STOPPING_DISTANCE < height:
            self.run(TICK_S)
        self.run(1.0, Intent())


class TestIntentParsing:
    def test_reads_held_keys(self):
        intent = Intent.from_payload({"up": True, "forward": True})
        assert intent.up and intent.forward
        assert not intent.down

    def test_unknown_keys_are_ignored(self):
        """A newer client sending an extra field must not drop the drone."""
        intent = Intent.from_payload({"up": True, "somethingNew": True, "x": 42})
        assert intent.up

    def test_non_object_is_rejected(self):
        with pytest.raises(ValueError, match="must be an object"):
            Intent.from_payload(["up"])

    def test_truthiness_is_coerced(self):
        assert Intent.from_payload({"up": 1}).up is True
        assert Intent.from_payload({"up": 0}).up is False


class TestArming:
    def test_idle_sends_nothing(self):
        rig = Rig()
        rig.run(1.0)
        assert rig.cmd.commands == []

    def test_armed_props_idle_gently_on_the_ground(self):
        rig = Rig()
        rig.ctl.arm()
        rig.run(0.2)
        assert rig.ctl.state is ControlState.ARMED
        assert rig.cmd.last("setpoint") == ("setpoint", 0.0, 0.0, 0.0, IDLE_THRUST)
        assert "hover" not in rig.cmd.kinds()

    def test_idle_thrust_is_just_above_no_power(self):
        # cflib: 10001 is "next to no power", 60000 is full.
        assert 10001 < IDLE_THRUST < 15000

    def test_down_while_armed_does_nothing(self):
        rig = Rig()
        rig.ctl.arm()
        rig.run(1.0, Intent(down=True))
        assert rig.ctl.state is ControlState.ARMED
        assert rig.ctl.target_height == 0.0


class TestGentleClimb:
    def test_holding_up_rises_at_the_gentle_rate(self):
        rig = Rig()
        rig.ctl.arm()
        rig.run(2.0, Intent(up=True))
        assert rig.ctl.target_height == pytest.approx(glide_distance(2.0), abs=0.01)
        assert rig.ctl.climb_velocity == pytest.approx(CLIMB_RATE_M_S)
        assert rig.ctl.state is ControlState.FLYING

    def test_height_is_commanded_relative_to_ground(self):
        rig = Rig()
        rig.fly_to(0.30)
        rig.run(0.1)
        assert rig.cmd.height() == pytest.approx(GROUND + 0.30, abs=0.015)

    def test_releasing_the_keys_holds_height(self):
        """The old controller bled thrust off and the drone sank."""
        rig = Rig()
        rig.fly_to(0.40)
        before = rig.ctl.target_height
        rig.run(3.0, Intent())
        assert rig.ctl.target_height == pytest.approx(before)
        assert rig.ctl.climb_velocity == 0.0
        assert rig.ctl.state is ControlState.FLYING

    def test_lowering_is_gentle_too(self):
        rig = Rig()
        rig.fly_to(0.60)
        start = rig.ctl.target_height
        rig.run(1.0, Intent(down=True))
        assert rig.ctl.target_height == pytest.approx(start - glide_distance(1.0), abs=0.01)

    def test_height_is_capped(self):
        rig = Rig()
        rig.ctl.arm()
        rig.run(MAX_HEIGHT_M / CLIMB_RATE_M_S + 5, Intent(up=True))
        assert rig.ctl.target_height == MAX_HEIGHT_M

    def test_a_long_stall_does_not_jump_the_height(self):
        rig = Rig()
        rig.fly_to(0.30)
        rig.ctl.set_intent(Intent(up=True))
        rig.clock.advance(0.4)        # one delayed tick
        rig.ctl.heartbeat()
        rig.ctl.tick()
        assert rig.ctl.target_height < 0.30 + 0.1 * CLIMB_RATE_M_S + 0.01


class TestMovement:
    def test_arrows_and_yaw_follow_cflib_conventions(self):
        # The fallback law, which is the only one that still puts velocities
        # on the wire for these conventions to be visible in.
        rig = Rig(fix=None)
        rig.fly_to(0.3)
        rig.run(1.0, Intent(forward=True, left=True, yaw_left=True))
        _, vx, vy, yaw, _ = rig.cmd.last("hover")
        assert vx == pytest.approx(KEY_MOVE_SPEED_M_S)
        assert vy == pytest.approx(KEY_MOVE_SPEED_M_S)     # left is +vy
        assert yaw == pytest.approx(YAW_RATE_DEG_S)    # turning left is +yawrate

    def test_opposite_keys_cancel(self):
        # No position source, so the cancelled velocities stay visible on the
        # wire instead of resolving into a held spot.
        rig = Rig(fix=None)
        rig.fly_to(0.3)
        rig.run(0.1, Intent(forward=True, back=True, yaw_left=True, yaw_right=True))
        _, vx, _, yaw, _ = rig.cmd.last("hover")
        assert vx == 0 and yaw == 0


class TestGracefulLanding:
    def test_land_hands_over_to_the_high_level_commander(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.ctl.land()
        rig.run(0.1)
        kinds = rig.cmd.kinds()
        assert kinds.index("notify_stop") > rig.cmd.first_flying()
        assert rig.lands == [(GROUND, LAND_S)]
        assert rig.ctl.state is ControlState.LANDING

    def test_no_low_level_setpoints_while_landing(self):
        """They would reclaim priority and cancel the landing (CLAUDE.md #2)."""
        rig = Rig()
        rig.fly_to(0.3)
        rig.ctl.land()
        rig.run(0.1)
        count = len(rig.cmd.commands)
        rig.run(1.0, Intent(up=True))
        assert rig.cmd.kinds()[count:] == []

    def test_landing_completes_and_can_arm_again(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.ctl.land()
        rig.run(LAND_S + 0.2)
        assert rig.ctl.state is ControlState.LANDED
        assert rig.cmd.kinds()[-2:] == ["stop", "notify_stop"]
        rig.ctl.arm()
        assert rig.ctl.state is ControlState.ARMED

    def test_lowering_to_the_floor_lands(self):
        rig = Rig()
        rig.fly_to(0.2)
        rig.run(2.0, Intent(down=True))
        assert rig.ctl.state in (ControlState.LANDING, ControlState.LANDED)
        assert rig.lands

    def test_land_while_only_idling_disarms(self):
        rig = Rig()
        rig.ctl.arm()
        rig.run(0.2)
        rig.ctl.land()
        assert rig.ctl.state is ControlState.IDLE
        assert rig.cmd.kinds()[-2:] == ["stop", "notify_stop"]
        assert rig.lands == []


    def test_a_landing_hands_priority_back_so_the_drone_does_not_lock(self):
        """A lone stop setpoint is the last one the firmware ever gets; with
        the motors above idle in the last 2 s its supervisor stays "flying",
        the commander watchdog fires and it LOCKS (supervisor.info 580 two
        seconds after every mission landed, 2026-11-20). The notify after the
        stop hands control to the stopped high-level commander, which keeps
        the setpoint fresh, so the supervisor goes landed -> ready."""
        rig = Rig()
        rig.fly_to(0.3)
        rig.ctl.land()
        rig.run(LAND_S + 0.2)
        kinds = rig.cmd.kinds()
        last_stop = len(kinds) - 1 - kinds[::-1].index("stop")
        assert kinds[last_stop + 1:] == ["notify_stop"]

    def test_shutting_the_loop_down_on_the_ground_releases_too(self):
        rig = Rig()
        rig.ctl.arm()
        rig.run(0.2)
        rig.ctl.stop()
        assert rig.cmd.kinds()[-2:] == ["stop", "notify_stop"]


class TestHeartbeat:
    def test_silence_while_flying_lands_gracefully(self):
        """The core safety property: a dead app lands the drone."""
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(HEARTBEAT_TIMEOUT_S + 0.1, heartbeat=False)
        assert rig.ctl.state is ControlState.LANDING
        assert rig.lands == [(GROUND, LAND_S)]
        assert rig.ctl.stats.heartbeat_timeouts == 1

    def test_silence_while_armed_disarms(self):
        rig = Rig()
        rig.ctl.arm()
        rig.run(HEARTBEAT_TIMEOUT_S + 0.1, heartbeat=False)
        assert rig.ctl.state is ControlState.IDLE
        assert rig.cmd.kinds()[-2:] == ["stop", "notify_stop"]

    def test_heartbeat_keeps_it_flying(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(5.0)
        assert rig.ctl.state is ControlState.FLYING
        assert rig.ctl.stats.heartbeat_timeouts == 0

    def test_timeout_does_not_fire_twice(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(2.0, heartbeat=False)
        assert rig.ctl.stats.heartbeat_timeouts == 1

    def test_late_input_does_not_cancel_a_landing(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(HEARTBEAT_TIMEOUT_S + 0.1, heartbeat=False)
        rig.run(0.5, Intent(up=True))          # the app reconnects
        assert rig.ctl.state in (ControlState.LANDING, ControlState.LANDED)


class TestEmergencyStop:
    def test_stops_immediately_without_waiting_for_a_tick(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.ctl.emergency_stop()
        assert rig.cmd.kinds()[-1] == "stop"
        assert rig.ctl.state is ControlState.STOPPED

    def test_an_emergency_stop_still_latches(self):
        """Not released: the emergency stop is meant to hold the motors, and
        Reset drone clears it (decision 2026-11-20)."""
        rig = Rig()
        rig.fly_to(0.3)
        count = len(rig.cmd.kinds())
        rig.ctl.emergency_stop()
        assert "notify_stop" not in rig.cmd.kinds()[count:]

    def test_nothing_is_sent_after_a_stop(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.ctl.emergency_stop()
        count = len(rig.cmd.commands)
        rig.run(1.0, Intent(up=True))
        assert len(rig.cmd.commands) == count

    def test_cannot_arm_again_until_reset(self):
        rig = Rig()
        rig.ctl.emergency_stop()
        with pytest.raises(RuntimeError, match="emergency stop"):
            rig.ctl.arm()
        rig.ctl.reset()
        rig.ctl.arm()
        assert rig.ctl.state is ControlState.ARMED

    def test_stop_is_counted(self):
        rig = Rig()
        rig.ctl.emergency_stop()
        assert rig.ctl.stats.emergency_stops == 1


class TestLoopRate:
    def test_holds_fifty_hertz_in_real_time(self):
        """The rate is the invariant: below ~10 Hz the commander drops the drone.

        Real thread, real sleep — the bug this guards against lives in the
        scheduling, which a fake clock cannot see. A fixed sleep after each tick
        measured 38 Hz. Bounds are loose enough for a busy machine and still
        fail that.
        """
        import time

        cmd = FakeCommander()
        ctl = ManualController(cmd, ground_z=GROUND, land=lambda z, d: None)
        ctl.arm()
        ctl.start()
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            ctl.heartbeat()
            time.sleep(0.05)
        ctl.land()                      # disarm on the ground
        ctl.stop()

        rate = cmd.kinds().count("setpoint") / 1.0
        assert 45 <= rate <= 55, f"loop ran at {rate:.0f} Hz"


class TestUnassisted:
    """No base stations: height from the barometer, held by the firmware.

    The throttle law this replaced let the drone skid through liftoff and
    tumble in the lab (2026-09-16), and letting go of W held nothing. These pin
    the behaviour the operator asked for instead: a gradual climb, a held
    height when the key is released, and a controlled descent — never a drop.
    """

    def test_w_climbs_gradually_on_a_height_setpoint(self):
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(1.0, Intent(up=True))
        assert rig.ctl.state is ControlState.FLYING
        assert rig.ctl.target_height == pytest.approx(glide_distance(1.0), abs=0.01)
        _, _, _, _, z = rig.cmd.last("zdistance")
        assert z == pytest.approx(GROUND + glide_distance(1.0), abs=0.01)
        assert "hover" not in rig.cmd.kinds()

    def test_releasing_w_holds_the_height(self):
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(2.0, Intent(up=True))
        released = rig.ctl.target_height
        rig.run(1.0, Intent())                    # glides to a stop
        settled = rig.ctl.target_height
        assert settled == pytest.approx(released + STOPPING_DISTANCE, abs=0.01)
        rig.run(3.0, Intent())                    # and then holds exactly
        assert rig.ctl.target_height == settled
        assert rig.cmd.last("zdistance")[4] == pytest.approx(GROUND + settled)

    def test_the_height_is_capped(self):
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(30.0, Intent(up=True))
        assert rig.ctl.target_height == MAX_UNASSISTED_HEIGHT_M
        assert all(c[4] <= GROUND + MAX_UNASSISTED_HEIGHT_M
                   for c in rig.cmd.commands if c[0] == "zdistance")

    def test_the_drone_is_kept_level_when_no_arrow_is_held(self):
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(2.0, Intent(up=True))
        _, roll, pitch, yaw, _ = rig.cmd.last("zdistance")
        assert (roll, pitch, yaw) == (0.0, 0.0, 0.0)

    def test_arrows_tilt_gently_and_never_beyond_the_limit(self):
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(1.0, Intent(up=True))
        rig.run(1.0, Intent(forward=True, right=True))
        _, roll, pitch, _, _ = rig.cmd.last("zdistance")
        assert abs(pitch) == MAX_TILT_DEG and roll == MAX_TILT_DEG
        assert all(abs(c[1]) <= MAX_TILT_DEG and abs(c[2]) <= MAX_TILT_DEG
                   for c in rig.cmd.commands if c[0] == "zdistance")

    def test_land_descends_at_a_fixed_rate_then_stops(self):
        """Land lowers the height target — a descent, not a motor cut — and
        never calls the high-level landing, which needs a position."""
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(3.0, Intent(up=True))
        start = rig.ctl.target_height
        rig.ctl.land()
        # Landing mid-climb eases out of the climb first, then into the descent.
        rig.run(2.0)
        assert rig.ctl.state is ControlState.LANDING
        assert rig.ctl.target_height < start - 0.1           # it is coming down
        assert rig.ctl.climb_velocity == pytest.approx(-LAND_RATE_M_S)

        rig.run(start / LAND_RATE_M_S + 1.0 + TOUCHDOWN_HOLD_S + 0.2)
        assert rig.ctl.state is ControlState.LANDED
        assert rig.lands == []
        assert rig.cmd.kinds()[-2:] == ["stop", "notify_stop"]

    def test_a_quiet_app_descends_rather_than_drops(self):
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(3.0, Intent(up=True))
        rig.run(HEARTBEAT_TIMEOUT_S + 0.2, heartbeat=False)
        assert rig.ctl.state is ControlState.LANDING
        assert rig.ctl.stats.heartbeat_timeouts >= 1
        assert rig.cmd.kinds()[-1] == "zdistance"

    def test_emergency_stop_cuts_immediately(self):
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(2.0, Intent(up=True))
        rig.ctl.emergency_stop()
        assert rig.cmd.kinds()[-1] == "stop"
        assert rig.ctl.state is ControlState.STOPPED

    def test_arming_idles_the_props_without_lifting(self):
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(0.2, Intent())
        assert rig.ctl.state is ControlState.ARMED
        assert rig.cmd.last("setpoint")[4] == IDLE_THRUST


class TestThrustUnlock:
    """The firmware holds thrust at zero until it sees one zero-thrust setpoint.

    Missing it cost a lab session: the app armed, reported the props turning,
    and no key moved the drone. Pinned for both control laws.
    """

    @pytest.mark.parametrize("assisted", [True, False])
    def test_arming_sends_the_unlock_before_any_thrust(self, assisted):
        rig = Rig(assisted=assisted)
        rig.ctl.arm()
        rig.run(1.0, Intent(up=True))
        setpoints = [c for c in rig.cmd.commands if c[0] == "setpoint"]
        assert setpoints[0] == ("setpoint", 0.0, 0.0, 0.0, 0)
        assert rig.cmd.commands[0] == ("setpoint", 0.0, 0.0, 0.0, 0)



class TestGlide:
    """Nothing the keys command changes in a step.

    The lab traces of 2026-09-17 show motors slamming between 0 and full every
    0.2–0.4 s. A climb speed that jumps on key press and release is a jolt the
    height controller answers with exactly that. These pin the easing.
    """

    @pytest.mark.parametrize("assisted", [True, False])
    def test_the_climb_speed_never_steps(self, assisted):
        rig = Rig(assisted=assisted)
        rig.ctl.arm()
        speeds = []
        plan = [(Intent(up=True), 2.0), (Intent(), 1.0), (Intent(down=True), 1.0), (Intent(), 1.0)]
        for keys, seconds in plan:
            rig.ctl.set_intent(keys)
            for _ in range(round(seconds / TICK_S)):
                rig.run(TICK_S)
                speeds.append(rig.ctl.climb_velocity)
        steps = [abs(b - a) for a, b in zip(speeds, speeds[1:], strict=False)]
        assert max(steps) <= CLIMB_ACCEL_M_S2 * TICK_S + 1e-9

    def test_releasing_w_eases_to_a_stop_rather_than_stopping_dead(self):
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(2.0, Intent(up=True))
        rig.run(0.1, Intent())
        assert 0 < rig.ctl.climb_velocity < CLIMB_RATE_M_S       # still slowing
        rig.run(1.0, Intent())
        assert rig.ctl.climb_velocity == 0.0

    def test_the_commanded_height_is_smooth(self):
        """Second difference of the setpoint bounded by the acceleration limit."""
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(2.0, Intent(up=True))
        rig.run(1.5, Intent())
        zs = [c[4] for c in rig.cmd.commands if c[0] == "zdistance"]
        triples = zip(zs, zs[1:], zs[2:], strict=False)
        accel = [abs((c - b) - (b - a)) / TICK_S**2 for a, b, c in triples]
        assert max(accel) <= CLIMB_ACCEL_M_S2 * 1.01

    def test_tilt_and_yaw_ease_in_and_out(self):
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(1.0, Intent(up=True))
        rig.run(0.1, Intent(right=True, yaw_left=True))
        _, roll, _, yaw, _ = rig.cmd.last("zdistance")
        assert 0 < roll < MAX_TILT_DEG
        assert 0 < yaw < YAW_RATE_DEG_S
        rig.run(0.5, Intent(right=True, yaw_left=True))     # inside the 1 s push
        _, roll, _, yaw, _ = rig.cmd.last("zdistance")
        assert roll == pytest.approx(MAX_TILT_DEG) and yaw == pytest.approx(YAW_RATE_DEG_S)
        rig.run(0.1, Intent())
        _, roll, _, yaw, _ = rig.cmd.last("zdistance")
        assert 0 < roll < MAX_TILT_DEG                           # levelling, not snapped

    def test_assisted_arrows_ease_too(self):
        rig = Rig(fix=None)                          # see the note above
        rig.fly_to(0.3)
        rig.run(0.1, Intent(forward=True))
        _, vx, _, _, _ = rig.cmd.last("hover")
        assert 0 < vx <= MOVE_ACCEL_M_S2 * 0.1 + 1e-9


class TestTheCommandedPoint:
    """The keys move a point; the drone is always told to be at that point.

    A speed says how fast and never says where, so nothing knew where the
    drone was supposed to be and sideways slip during a move was invisible.
    Every test here is about the point: what moves it, what must not, and
    what the drone is told while it moves.
    """

    def test_a_held_arrow_advances_the_point_forward_and_nothing_else(self):
        rig = Rig()
        rig.fly_to(0.3)
        before = rig.ctl.target
        rig.run(1.0, Intent(forward=True))
        after = rig.ctl.target
        assert after.x > before.x                    # travelled forward
        assert after.y == pytest.approx(before.y)    # and not sideways
        assert after.yaw_deg == pytest.approx(before.yaw_deg)

    def test_the_point_is_what_the_drone_is_told(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(1.0, Intent(forward=True))
        target = rig.ctl.target
        _, x, y, z, yaw = rig.cmd.last("position")
        assert (x, y, yaw) == (target.x, target.y, target.yaw_deg)
        assert z == pytest.approx(GROUND + rig.ctl.target_height)

    def test_the_point_advances_only_while_the_key_is_held(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(1.0, Intent(forward=True))
        rig.run(1.0, Intent())                       # released: ease to a stop
        stopped = rig.ctl.target
        rig.run(2.0, Intent())
        assert rig.ctl.target == stopped             # and it stays stopped
        assert rig.cmd.kinds()[-1] == "position"     # still being commanded

    def test_up_climbs_straight_up(self):
        """The bug that started this: W sent vx=vy=0, which asks the drone to
        stop moving sideways but never asks it to stay anywhere."""
        rig = Rig()
        rig.fly_to(0.3)
        before = rig.ctl.target
        height_before = rig.ctl.target_height
        rig.run(1.0, Intent(up=True))
        assert rig.ctl.target_height > height_before  # it climbed
        assert rig.ctl.target == before               # and held x, y and heading

    def test_yaw_turns_the_heading_and_holds_the_place(self):
        rig = Rig()
        rig.fly_to(0.3)
        before = rig.ctl.target
        rig.run(1.0, Intent(yaw_left=True))
        after = rig.ctl.target
        assert after.yaw_deg > before.yaw_deg
        assert (after.x, after.y) == (before.x, before.y)


class TestArrowsIgnoreTheNose:
    """The arrows move the drone in the ROOM — away from the operator, or
    along the room's own directions — whatever way its nose points
    (keyframe.py; the owner, 2026-09-30). Until then ↑ meant "where the nose
    points", and a drone set down at -116° (flight-log.txt) or turned with A/D
    went somewhere the operator did not expect."""

    HEADINGS = (0.0, 90.0, 180.0, -116.0, 37.0)

    @staticmethod
    def moved(rig: Rig, before: Fix) -> tuple[float, float]:
        target = rig.ctl.target
        return (target.x - before.x, target.y - before.y)

    @pytest.mark.parametrize("heading", HEADINGS)
    def test_room_forward_is_plus_x_whatever_the_heading(self, heading):
        rig = Rig(fix=Fix(0.0, 0.0, heading), key_frame=KeyFrame.ROOM)
        rig.fly_to(0.3)
        before = rig.ctl.target
        rig.run(1.0, Intent(forward=True))
        dx, dy = self.moved(rig, before)
        assert dx > 0.02 and dy == pytest.approx(0.0, abs=1e-9)

    @pytest.mark.parametrize("heading", HEADINGS)
    def test_room_left_is_plus_y_whatever_the_heading(self, heading):
        rig = Rig(fix=Fix(0.0, 0.0, heading), key_frame=KeyFrame.ROOM)
        rig.fly_to(0.3)
        before = rig.ctl.target
        rig.run(1.0, Intent(left=True))
        dx, dy = self.moved(rig, before)
        assert dy > 0.02 and dx == pytest.approx(0.0, abs=1e-9)

    def test_turning_with_a_and_d_does_not_turn_the_arrows(self):
        rig = Rig(fix=Fix(0.0, 0.0, 0.0), key_frame=KeyFrame.ROOM)
        rig.fly_to(0.3)
        rig.run(3.0, Intent(yaw_left=True))           # 90 degrees round
        assert rig.ctl.target.yaw_deg > 60.0
        before = rig.ctl.target
        rig.run(1.0, Intent(forward=True))
        dx, dy = self.moved(rig, before)
        assert dx > 0.02 and dy == pytest.approx(0.0, abs=1e-9)

    @pytest.mark.parametrize("heading", HEADINGS)
    @pytest.mark.parametrize(("where", "away"), [
        ((1.0, 0.0), (1.0, 0.0)),            # in front of the operator
        ((0.0, -1.5), (0.0, -1.0)),          # behind them: they turn to face it
        ((-1.0, 1.0), (-0.7071, 0.7071)),    # off to one side, diagonally
    ])
    def test_up_flies_it_away_from_the_operator(self, heading, where, away):
        rig = Rig(fix=Fix(where[0], where[1], heading), operator=(0.0, 0.0))
        rig.fly_to(0.3)
        before = rig.ctl.target
        rig.run(1.0, Intent(forward=True))
        dx, dy = self.moved(rig, before)
        length = math.hypot(dx, dy)
        assert length > 0.02
        assert (dx / length, dy / length) == pytest.approx(away, abs=1e-3)
        assert rig.ctl.frame_status.active is ActiveFrame.OPERATOR

    def test_down_brings_it_back_towards_the_operator(self):
        rig = Rig(fix=Fix(1.0, 0.0, 145.0), operator=(0.0, 0.0))
        rig.fly_to(0.3)
        before = rig.ctl.target
        rig.run(1.0, Intent(back=True))
        dx, dy = self.moved(rig, before)
        assert dx < -0.02 and dy == pytest.approx(0.0, abs=1e-9)

    def test_left_goes_round_to_the_operators_left(self):
        rig = Rig(fix=Fix(1.0, 0.0, 145.0), operator=(0.0, 0.0))
        rig.fly_to(0.3)
        before = rig.ctl.target
        rig.run(1.0, Intent(left=True))
        dx, dy = self.moved(rig, before)
        # Facing the drone along +x, the operator's left is +y.
        assert dy > 0.02 and dx == pytest.approx(0.0, abs=1e-9)

    def test_without_a_marked_spot_away_is_measured_from_the_takeoff_spot(self):
        rig = Rig(fix=Fix(0.0, 0.0, 70.0))
        rig.fly_to(0.3)
        assert rig.ctl.frame_status.operator == (0.0, 0.0)
        assert rig.ctl.frame_status.operator_source == "takeoff"
        rig.drift(0.0, -1.0)                          # it wanders 1 m to -y
        before = rig.ctl.target
        rig.run(0.5, Intent(forward=True))
        dx, dy = self.moved(rig, before)
        assert dy < -0.01 and abs(dx) < 0.005         # further along -y
        assert rig.ctl.frame_status.active is ActiveFrame.OPERATOR

    def test_close_to_the_operator_the_arrows_use_the_room(self):
        rig = Rig(fix=Fix(0.2, 0.1, 150.0), operator=(0.0, 0.0))
        rig.fly_to(0.3)
        status = rig.ctl.frame_status
        assert status.active is ActiveFrame.ROOM
        assert status.reason is Reason.NEAR_OPERATOR
        before = rig.ctl.target
        rig.run(0.5, Intent(forward=True))
        dx, dy = self.moved(rig, before)
        assert dx > 0.01 and dy == pytest.approx(0.0, abs=1e-9)

    def test_the_near_circle_has_hysteresis(self):
        rig = Rig(fix=Fix(NEAR_OPERATOR_M - 0.05, 0.0, 0.0), operator=(0.0, 0.0))
        rig.fly_to(0.3)
        assert rig.ctl.frame_status.reason is Reason.NEAR_OPERATOR
        # Between the two radii it stays near — no flicker at the edge.
        rig.fix = Fix((NEAR_OPERATOR_M + FAR_OPERATOR_M) / 2, 0.0, 0.0)
        rig.run(0.1)
        assert rig.ctl.frame_status.reason is Reason.NEAR_OPERATOR
        rig.fix = Fix(FAR_OPERATOR_M + 0.05, 0.0, 0.0)
        rig.run(0.1)
        assert rig.ctl.frame_status.active is ActiveFrame.OPERATOR

    def test_room_mode_ignores_the_operator(self):
        rig = Rig(fix=Fix(0.0, -1.5, 0.0), key_frame=KeyFrame.ROOM, operator=(0.0, 0.0))
        rig.fly_to(0.3)
        before = rig.ctl.target
        rig.run(1.0, Intent(forward=True))
        dx, dy = self.moved(rig, before)
        assert dx > 0.02 and dy == pytest.approx(0.0, abs=1e-9)
        assert rig.ctl.frame_status.active is ActiveFrame.ROOM
        assert rig.ctl.frame_status.reason is None

    def test_switching_frames_in_the_air_takes_effect_and_is_announced(self):
        rig = Rig(fix=Fix(0.0, -1.5, 0.0), operator=(0.0, 0.0))
        rig.fly_to(0.3)
        heard = len(rig.frames)
        rig.ctl.set_key_frame(KeyFrame.ROOM, (0.0, 0.0))
        assert len(rig.frames) == heard + 1
        assert rig.frames[-1].active is ActiveFrame.ROOM
        before = rig.ctl.target
        rig.run(1.0, Intent(forward=True))
        dx, dy = self.moved(rig, before)
        assert dx > 0.02 and dy == pytest.approx(0.0, abs=1e-9)

    def test_the_app_hears_changes_not_every_tick(self):
        rig = Rig(fix=Fix(1.0, 0.0, 0.0), operator=(0.0, 0.0))
        rig.fly_to(0.3)
        heard = len(rig.frames)
        rig.run(2.0, Intent(left=True))    # circles round: the angle moves...
        assert len(rig.frames) == heard    # ...but the frame is the same news

    def test_a_listener_that_raises_does_not_stop_the_loop(self):
        rig = Rig(fix=Fix(1.0, 0.0, 0.0), operator=(0.0, 0.0))

        def broken(_status):
            raise RuntimeError("listener bug")

        rig.ctl._on_frame_change = broken
        rig.fly_to(0.3)
        rig.run(0.5, Intent(forward=True))
        assert rig.ctl.state is ControlState.FLYING

    def test_landing_clears_what_the_arrows_mean(self):
        rig = Rig(fix=Fix(1.0, 0.0, 0.0), operator=(0.0, 0.0))
        rig.fly_to(0.3)
        assert rig.ctl.frame_status is not None
        rig.ctl.land()
        assert rig.ctl.frame_status is None
        assert rig.frames[-1] is None

    def test_no_position_keeps_the_takeoff_direction_as_the_drone_turns(self):
        # Assisted, but the position never arrives: the velocity fallback,
        # which is in the drone's frame — the arrows are turned into it.
        rig = Rig(fix=None, yaw=0.0)
        rig.fly_to(0.3)
        rig.yaw = 90.0                           # it has turned a quarter left
        rig.run(1.0, Intent(forward=True))
        _, vx, vy, _, _ = rig.cmd.last("hover")
        # Still the takeoff direction (+x), which is now the drone's right.
        assert vx == pytest.approx(0.0, abs=1e-9)
        assert vy == pytest.approx(-KEY_MOVE_SPEED_M_S)
        status = rig.ctl.frame_status
        assert status.active is ActiveFrame.TAKEOFF and status.reason is Reason.NO_POSITION

    def test_no_heading_at_all_falls_back_to_the_nose(self):
        rig = Rig(fix=None, yaw=None)
        rig.fly_to(0.3)
        rig.run(1.0, Intent(forward=True))
        _, vx, vy, _, _ = rig.cmd.last("hover")
        assert vx == pytest.approx(KEY_MOVE_SPEED_M_S) and vy == 0
        assert rig.ctl.frame_status.active is ActiveFrame.NOSE
        assert rig.ctl.frame_status.reason is Reason.NO_HEADING


class TestUnassistedArrowsKeepTheTakeoffDirection:
    """No base stations: no room and no operator to be away from. The arrows
    keep the way the nose pointed at takeoff, however the drone turns."""

    def test_after_a_quarter_turn_up_still_tilts_towards_the_takeoff_direction(self):
        rig = Rig(assisted=False, fix=Fix(0.0, 0.0, 0.0))
        rig.fly_to(0.3)
        rig.fix = Fix(0.0, 0.0, 90.0)             # turned left a quarter
        rig.run(1.0, Intent(forward=True))
        _, roll, pitch, _, _ = rig.cmd.last("zdistance")
        # The takeoff direction is now the drone's right: roll right, no pitch.
        assert roll == pytest.approx(MAX_TILT_DEG)
        assert pitch == pytest.approx(0.0, abs=1e-9)
        assert rig.ctl.frame_status.active is ActiveFrame.TAKEOFF

    def test_facing_the_takeoff_direction_it_tilts_as_it_always_did(self):
        rig = Rig(assisted=False, fix=Fix(0.0, 0.0, -116.0))
        rig.fly_to(0.3)
        rig.run(1.0, Intent(forward=True))
        _, roll, pitch, _, _ = rig.cmd.last("zdistance")
        assert roll == pytest.approx(0.0, abs=1e-9)
        assert pitch == pytest.approx(MAX_TILT_DEG * -1.0)   # PITCH_SIGN

    def test_with_no_heading_it_tilts_along_the_nose(self):
        rig = Rig(assisted=False, fix=None, yaw=None)
        rig.fly_to(0.3)
        rig.run(1.0, Intent(right=True))
        _, roll, _, _, _ = rig.cmd.last("zdistance")
        assert roll == pytest.approx(MAX_TILT_DEG)


class TestHeading:
    """The heading is only the nose: A and D turn it, and it is held."""

    def test_the_heading_stays_inside_half_a_turn(self):
        """A heading that accumulates past 180 is still a heading, but the
        firmware reads an absolute one and the number should read like it."""
        rig = Rig(fix=Fix(0.0, 0.0, 170.0))
        rig.fly_to(0.3)
        rig.run(2.0, Intent(yaw_left=True))          # 30 deg/s past the wrap
        assert -180.0 <= rig.ctl.target.yaw_deg < 180.0
        assert rig.ctl.target.yaw_deg < 0            # wrapped, not 200


class TestMultipleKeys:
    """Each axis reads only its own pair, so the axes never interfere."""

    def test_opposite_arrows_hold_the_place(self):
        rig = Rig()
        rig.fly_to(0.3)
        held = rig.ctl.target
        rig.run(1.0, Intent(forward=True, back=True))
        assert rig.ctl.target == held
        assert rig.cmd.kinds()[-1] == "position"     # held, not merely stopped

    def test_up_and_down_together_hold_the_level(self):
        rig = Rig()
        rig.fly_to(0.3)
        height = rig.ctl.target_height
        rig.run(1.0, Intent(up=True, down=True))
        assert rig.ctl.target_height == pytest.approx(height)

    def test_opposite_yaw_holds_the_heading(self):
        rig = Rig()
        rig.fly_to(0.3)
        held = rig.ctl.target
        rig.run(1.0, Intent(yaw_left=True, yaw_right=True))
        assert rig.ctl.target.yaw_deg == pytest.approx(held.yaw_deg)

    def test_two_different_axes_both_move(self):
        rig = Rig()
        rig.fly_to(0.3)
        before, height = rig.ctl.target, rig.ctl.target_height
        rig.run(1.0, Intent(up=True, left=True))
        assert rig.ctl.target_height > height        # climbing
        assert rig.ctl.target.y > before.y           # and sliding left

    def test_a_cancelled_axis_does_not_freeze_a_live_one(self):
        rig = Rig()
        rig.fly_to(0.3)
        before, height = rig.ctl.target, rig.ctl.target_height
        rig.run(1.0, Intent(up=True, down=True, forward=True))
        assert rig.ctl.target_height == pytest.approx(height)   # level held
        assert rig.ctl.target.x > before.x                      # still travelling

    def test_cancelling_mid_move_stops_smoothly_not_instantly(self):
        """The rate eases to zero, so the point runs on a few centimetres and
        stops somewhere definite. A freeze would be a jolt."""
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(1.0, Intent(forward=True))
        moving = rig.ctl.target
        rig.run(TICK_S, Intent(forward=True, back=True))
        assert rig.ctl.target.x > moving.x           # still easing down
        rig.run(1.0, Intent(forward=True, back=True))
        settled = rig.ctl.target
        rig.run(1.0, Intent(forward=True, back=True))
        assert rig.ctl.target == settled             # and then it is still


class TestTheLeash:
    """The point may not outrun the drone.

    Holding a key advances the point whether or not the drone keeps up. The
    fake drone here never moves, which is the extreme of exactly that.
    """

    def test_the_point_never_gets_further_than_the_leash(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(10.0, Intent(forward=True))          # 2 m of asking
        gap = math.hypot(rig.ctl.target.x - rig.fix.x, rig.ctl.target.y - rig.fix.y)
        assert gap == pytest.approx(LEASH_M, abs=0.01)

    def test_the_leash_does_not_bite_during_ordinary_flight(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(1.0, Intent(forward=True))
        gap = math.hypot(rig.ctl.target.x - rig.fix.x, rig.ctl.target.y - rig.fix.y)
        assert gap < LEASH_M

    def test_the_leash_keeps_the_direction_it_was_asked_for(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(10.0, Intent(left=True))
        target = rig.ctl.target
        assert target.y - rig.fix.y == pytest.approx(LEASH_M, abs=0.01)
        assert target.x == pytest.approx(rig.fix.x, abs=1e-9)


class TestDrift:
    def test_the_distance_off_the_point_is_reported(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(1.0, Intent())
        assert rig.ctl.drift_m == pytest.approx(0.0, abs=1e-9)
        rig.drift(0.06, 0.08)                        # 3-4-5: exactly 10 cm off
        assert rig.ctl.drift_m == pytest.approx(0.10, abs=0.005)

    def test_the_drone_drifting_does_not_move_the_point(self):
        """The whole idea: the drone comes back to the point, not the other
        way round. A point that followed the live position would follow the
        drift and correct nothing."""
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(1.0, Intent())
        held = rig.ctl.target
        rig.drift(0.10, 0.0)
        assert rig.ctl.target == held
        _, x, y, _, _ = rig.cmd.last("position")
        assert (x, y) == (held.x, held.y)


class TestWhatTheEstimatorSays:
    def test_a_position_that_jumps_is_not_believed(self):
        """Stale geometry moved the estimate 21 cm in one 0.1 s step while the
        drone sat still. Chasing that would fly it hard into the error."""
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(1.0, Intent())
        held = rig.ctl.target
        rig.fix = Fix(held.x + 0.21, held.y, held.yaw_deg)   # 10.5 m/s
        rig.run(TICK_S)
        assert rig.ctl.drift_m == 0.0                # the jump never became drift
        assert rig.ctl.target == held

    def test_a_position_that_stays_put_is_believed_in_the_end(self):
        """Rejecting a jump must not blind the loop for good: a drone really
        carried somewhere keeps reporting it, and that is not a glitch."""
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(1.0, Intent())
        held = rig.ctl.target
        rig.fix = Fix(held.x + 0.21, held.y, held.yaw_deg)
        rig.run(0.5)                                 # same reading, tick after tick
        assert rig.ctl.drift_m == pytest.approx(0.21, abs=0.02)
        assert rig.ctl.target == held                # and still the original point

    def test_without_a_position_it_flies_by_velocity(self):
        rig = Rig(fix=None)
        rig.fly_to(0.3)
        rig.run(1.0, Intent(forward=True))
        assert "position" not in rig.cmd.kinds()
        assert rig.ctl.target is None
        _, vx, _, _, _ = rig.cmd.last("hover")
        assert vx == pytest.approx(KEY_MOVE_SPEED_M_S)

    def test_unassisted_flight_never_commands_a_point(self):
        """No base stations, no position worth flying to."""
        rig = Rig(assisted=False)
        rig.ctl.arm()
        rig.run(2.0, Intent(up=True))
        rig.run(2.0, Intent())
        assert "position" not in rig.cmd.kinds()
        assert rig.cmd.kinds()[-1] == "zdistance"

    def test_arming_again_forgets_the_old_point(self):
        rig = Rig()
        rig.fly_to(0.3)
        rig.run(1.0, Intent())
        assert rig.ctl.target is not None
        rig.ctl.land()
        rig.run(LAND_S + 0.5)
        rig.ctl.arm()
        assert rig.ctl.target is None



class TestHoldAt:
    """Rise to a height and hold it, without the operator holding W.

    The point of the feature is that it adds no control law: it asks the same
    glide the W key asks for, so everything already pinned about the glide,
    the ceiling, the guards and Land keeps applying. These tests pin the part
    that is new — that it arrives, that it stops, and that the operator can
    always take it back.
    """

    def test_it_climbs_to_the_goal_and_stops_there(self):
        rig = Rig()
        rig.ctl.arm()
        rig.ctl.hold_at(0.40)
        # Long enough to cover the eased climb plus its ease-out.
        rig.run(6.0)
        assert rig.ctl.target_height == pytest.approx(0.40, abs=0.02)
        assert rig.ctl.state is ControlState.FLYING

    def test_it_holds_after_arriving_without_being_asked_again(self):
        rig = Rig()
        rig.ctl.arm()
        rig.ctl.hold_at(0.35)
        rig.run(6.0)
        arrived = rig.ctl.target_height
        rig.run(4.0)                       # no keys, no goal
        assert rig.ctl.target_height == pytest.approx(arrived, abs=0.001)

    def test_it_descends_to_a_lower_goal(self):
        rig = Rig()
        rig.fly_to(0.60)
        rig.ctl.hold_at(0.25)
        rig.run(6.0)
        assert rig.ctl.target_height == pytest.approx(0.25, abs=0.02)

    def test_the_climb_is_eased_not_a_jump(self):
        """One tick must not move the target more than the climb rate allows."""
        rig = Rig()
        rig.ctl.arm()
        rig.ctl.hold_at(0.80)
        rig.run(TICK_S)
        assert rig.ctl.target_height <= CLIMB_RATE_M_S * TICK_S + 1e-6

    def test_holding_w_takes_over_from_the_goal(self):
        rig = Rig()
        rig.ctl.arm()
        rig.ctl.hold_at(0.20)
        rig.run(0.5, intent=Intent(up=True))
        rig.run(4.0, intent=Intent(up=True))
        # The goal was 0.20; W kept climbing well past it.
        assert rig.ctl.target_height > 0.30

    def test_holding_s_cancels_the_goal(self):
        rig = Rig()
        rig.fly_to(0.50)
        rig.ctl.hold_at(0.80)
        rig.run(0.4, intent=Intent(down=True))
        rig.run(1.0, intent=Intent())       # released: the goal must be gone
        assert rig.ctl.target_height < 0.50

    def test_landing_cancels_the_goal(self):
        rig = Rig()
        rig.fly_to(0.50)
        rig.ctl.hold_at(0.80)
        rig.ctl.land()
        rig.run(0.5)
        assert rig.ctl.state in (ControlState.LANDING, ControlState.LANDED)

    def test_an_emergency_stop_cancels_the_goal(self):
        rig = Rig()
        rig.fly_to(0.50)
        rig.ctl.hold_at(0.80)
        rig.ctl.emergency_stop()
        rig.run(0.5)
        assert rig.ctl.state is ControlState.STOPPED

    def test_it_will_not_exceed_the_assisted_ceiling(self):
        rig = Rig()
        rig.ctl.arm()
        with pytest.raises(ValueError, match="at most"):
            rig.ctl.hold_at(MAX_HEIGHT_M + 0.01)

    def test_the_unassisted_ceiling_is_lower(self):
        rig = Rig(assisted=False, fix=None)
        rig.ctl.arm()
        with pytest.raises(ValueError, match="at most"):
            rig.ctl.hold_at(MAX_UNASSISTED_HEIGHT_M + 0.01)

    def test_it_works_unassisted(self):
        """The whole reason this exists: Auto is refused without base stations,
        so the one-press hover has to work on the barometer."""
        rig = Rig(assisted=False, fix=None)
        rig.ctl.arm()
        rig.ctl.hold_at(0.40)
        rig.run(6.0)
        assert rig.ctl.target_height == pytest.approx(0.40, abs=0.02)

    def test_it_is_refused_before_the_props_are_running(self):
        rig = Rig()
        with pytest.raises(RuntimeError, match="not running"):
            rig.ctl.hold_at(0.30)

    def test_a_zero_or_negative_height_is_refused(self):
        rig = Rig()
        rig.ctl.arm()
        for bad in (0.0, -0.2):
            with pytest.raises(ValueError):
                rig.ctl.hold_at(bad)

    def test_the_heartbeat_still_lands_it_mid_climb(self):
        """A goal must not outlive the window that asked for it."""
        rig = Rig()
        rig.fly_to(0.40)
        rig.ctl.hold_at(0.80)
        rig.run(HEARTBEAT_TIMEOUT_S + 0.2, heartbeat=False)
        assert rig.ctl.state in (ControlState.LANDING, ControlState.LANDED)


class TestFlyTo:
    """fly_to() — the goal the mission controller flies a mission with.

    The drone here FOLLOWS the commanded point (the estimator reports wherever
    the last position setpoint put it), because a goal the drone never moves
    towards is held back by the leash — which one test below relies on.
    """

    @staticmethod
    def airborne(rig: Rig, height: float = 0.40) -> None:
        rig.ctl.arm()
        rig.ctl.hold_at(height)
        rig.run(6.0)
        assert rig.ctl.state is ControlState.FLYING

    @staticmethod
    def follow(rig: Rig, seconds: float, intent: Intent | None = None) -> None:
        """Tick with the drone tracking the commanded point perfectly."""
        if intent is not None:
            rig.ctl.set_intent(intent)
        for _ in range(round(seconds / TICK_S)):
            rig.clock.advance(TICK_S)
            rig.ctl.heartbeat()
            rig.ctl.tick()
            _, x, y, _, yaw = rig.cmd.last("position")
            rig.fix = Fix(x, y, yaw)

    def test_it_glides_to_the_point_and_holds_it(self):
        rig = Rig()
        self.airborne(rig)
        goal = (SOMEWHERE.x + 1.0, SOMEWHERE.y + 0.5)
        rig.ctl.fly_to(*goal, 0.60)
        assert rig.ctl.goal_active
        self.follow(rig, 12.0)
        _, x, y, z, _ = rig.cmd.last("position")
        assert math.hypot(x - goal[0], y - goal[1]) <= 0.02
        assert z == pytest.approx(GROUND + 0.60, abs=0.02)
        assert not rig.ctl.goal_active
        assert not rig.ctl.operator_override

    def test_it_never_goes_faster_than_the_arrow_keys(self):
        rig = Rig()
        self.airborne(rig)
        rig.ctl.fly_to(SOMEWHERE.x + 2.0, SOMEWHERE.y, 0.40)
        previous = rig.cmd.last("position")
        worst = 0.0
        for _ in range(round(8.0 / TICK_S)):
            self.follow(rig, TICK_S)
            current = rig.cmd.last("position")
            worst = max(worst, math.hypot(current[1] - previous[1],
                                          current[2] - previous[2]) / TICK_S)
            previous = current
        assert worst <= MOVE_SPEED_M_S + 1e-6

    @pytest.mark.parametrize("speed", [0.10, 0.15])
    def test_a_missions_slower_speed_is_kept_to(self, speed):
        rig = Rig()
        self.airborne(rig)
        rig.ctl.fly_to(SOMEWHERE.x + 2.0, SOMEWHERE.y, 0.40, speed_m_s=speed)
        previous = rig.cmd.last("position")
        worst = 0.0
        for _ in range(round(8.0 / TICK_S)):
            self.follow(rig, TICK_S)
            current = rig.cmd.last("position")
            worst = max(worst, math.hypot(current[1] - previous[1],
                                          current[2] - previous[2]) / TICK_S)
            previous = current
        assert worst <= speed + 1e-6
        assert worst >= speed * 0.9           # it does travel at it, once eased in

    @pytest.mark.parametrize("speed", [0.0, -0.1, 0.21, math.inf, math.nan])
    def test_a_speed_outside_zero_to_the_arrow_speed_is_refused(self, speed):
        rig = Rig()
        self.airborne(rig)
        with pytest.raises(ValueError, match="speed"):
            rig.ctl.fly_to(SOMEWHERE.x + 1.0, SOMEWHERE.y, 0.40, speed_m_s=speed)
        assert not rig.ctl.goal_active

    def test_it_does_not_overshoot(self):
        """The approach profile: the point slows before the goal, so it never
        runs past it by the easing's stopping distance."""
        rig = Rig()
        self.airborne(rig)
        goal_x = SOMEWHERE.x + 1.0
        rig.ctl.fly_to(goal_x, SOMEWHERE.y, 0.40)
        furthest = -math.inf
        for _ in range(round(12.0 / TICK_S)):
            self.follow(rig, TICK_S)
            furthest = max(furthest, rig.cmd.last("position")[1])
        assert furthest <= goal_x + 0.02

    def test_it_travels_the_right_way_whatever_the_heading(self):
        """The goal is in the ROOM's frame; the drone's heading must not bend it."""
        rig = Rig(fix=Fix(0.0, 0.0, 90.0))
        self.airborne(rig)
        rig.ctl.fly_to(1.0, 0.0, 0.40)
        self.follow(rig, 12.0)
        _, x, y, _, _ = rig.cmd.last("position")
        assert x == pytest.approx(1.0, abs=0.02)
        assert y == pytest.approx(0.0, abs=0.02)

    def test_a_held_key_cancels_it_and_says_so(self):
        rig = Rig()
        self.airborne(rig)
        rig.ctl.fly_to(SOMEWHERE.x + 2.0, SOMEWHERE.y, 0.40)
        self.follow(rig, 1.0)
        self.follow(rig, TICK_S, Intent(left=True))
        assert rig.ctl.operator_override
        assert not rig.ctl.goal_active
        # Released: the point stays where the operator left it, not the goal.
        self.follow(rig, 3.0, Intent())
        _, x, _, _, _ = rig.cmd.last("position")
        assert x < SOMEWHERE.x + 1.0

    def test_any_key_cancels_a_height_goal_too(self):
        rig = Rig()
        self.airborne(rig, 0.30)
        rig.ctl.fly_to(SOMEWHERE.x, SOMEWHERE.y, 0.90)
        self.follow(rig, 0.5)
        self.follow(rig, TICK_S, Intent(yaw_left=True))
        assert rig.ctl.operator_override
        assert not rig.ctl.goal_active

    def test_the_next_goal_clears_the_override(self):
        rig = Rig()
        self.airborne(rig)
        rig.ctl.fly_to(SOMEWHERE.x + 1.0, SOMEWHERE.y, 0.40)
        self.follow(rig, TICK_S, Intent(forward=True))
        self.follow(rig, TICK_S, Intent())
        assert rig.ctl.operator_override
        rig.ctl.fly_to(SOMEWHERE.x, SOMEWHERE.y, 0.40)
        assert not rig.ctl.operator_override

    def test_the_leash_still_holds_a_drone_that_does_not_follow(self):
        """The drone stuck where it is: the commanded point never gets more
        than the leash ahead of it, goal or not."""
        rig = Rig()
        self.airborne(rig)
        rig.ctl.fly_to(SOMEWHERE.x + 3.0, SOMEWHERE.y, 0.40)
        rig.run(10.0)
        _, x, y, _, _ = rig.cmd.last("position")
        assert math.hypot(x - SOMEWHERE.x, y - SOMEWHERE.y) <= LEASH_M + 1e-6

    def test_it_is_refused_unassisted(self):
        rig = Rig(assisted=False, fix=None)
        rig.ctl.arm()
        rig.ctl.hold_at(0.40)
        rig.run(6.0)
        with pytest.raises(RuntimeError, match="position"):
            rig.ctl.fly_to(1.0, 0.0, 0.40)

    def test_it_is_refused_on_the_ground(self):
        rig = Rig()
        rig.ctl.arm()
        with pytest.raises(RuntimeError, match="airborne"):
            rig.ctl.fly_to(1.0, 0.0, 0.40)

    def test_an_impossible_height_or_coordinate_is_refused(self):
        rig = Rig()
        self.airborne(rig)
        with pytest.raises(ValueError):
            rig.ctl.fly_to(1.0, 0.0, MAX_HEIGHT_M + 0.01)
        with pytest.raises(ValueError):
            rig.ctl.fly_to(math.nan, 0.0, 0.40)

    def test_landing_ends_the_goal(self):
        rig = Rig()
        self.airborne(rig)
        rig.ctl.fly_to(SOMEWHERE.x + 2.0, SOMEWHERE.y, 0.40)
        rig.ctl.land()
        assert not rig.ctl.goal_active

    def test_the_heartbeat_still_lands_it_mid_leg(self):
        """A mission does not defeat the dead-man: the app goes quiet, it lands."""
        rig = Rig()
        self.airborne(rig)
        rig.ctl.fly_to(SOMEWHERE.x + 2.0, SOMEWHERE.y, 0.40)
        rig.run(HEARTBEAT_TIMEOUT_S + 0.2, heartbeat=False)
        assert rig.ctl.state in (ControlState.LANDING, ControlState.LANDED)


class TestGentleArrows:
    """The owner, 2026-09-30: fly more steadily. One setting, no switch: the
    arrows lean half as far as before and one press pushes for at most a
    second. W / S and A / D are as they always were; so are missions."""

    def test_the_numbers(self):
        assert (MAX_TILT_DEG, TILT_RATE_DEG_S) == (2.5, 8.0)
        assert (KEY_MOVE_SPEED_M_S, MOVE_SPEED_M_S) == (0.12, 0.20)
        assert (CLIMB_RATE_M_S, YAW_RATE_DEG_S) == (0.15, 30.0)       # unchanged
        # Half the push of the old 5° lean: g*tan(2.5°) / g*tan(5°).
        assert math.tan(math.radians(2.5)) / math.tan(math.radians(5.0)) == pytest.approx(
            0.499, abs=0.002)

    @staticmethod
    def airborne() -> Rig:
        rig = Rig(assisted=False, fix=Fix(0.0, 0.0, 0.0))
        rig.fly_to(0.3)
        return rig

    def test_the_lean_builds_at_eight_degrees_a_second(self):
        rig = self.airborne()
        rig.run(0.2, Intent(forward=True))
        assert rig.cmd.last("zdistance")[2] == pytest.approx(-8.0 * 0.2, abs=0.01)
        rig.run(0.4)
        assert rig.cmd.last("zdistance")[2] == pytest.approx(-MAX_TILT_DEG)

    def test_a_held_arrow_leans_for_as_long_as_it_is_held(self):
        """The lab, 2026-09-30 (trace_7b36d994): ↓ held 5 s to bring back a
        drifting drone. A lean that fades under a held key is lost control."""
        rig = self.airborne()
        for _ in range(10):                                  # 5 s, looked at every 0.5 s
            rig.run(0.5, Intent(back=True))
        pitches = [c[2] for c in rig.cmd.commands if c[0] == "zdistance"][-200:]
        assert min(pitches) == pytest.approx(MAX_TILT_DEG)   # 4 s of it, never easing off
        assert max(pitches) == pytest.approx(MAX_TILT_DEG)

    def test_letting_go_levels_it(self):
        rig = self.airborne()
        rig.run(2.0, Intent(forward=True))
        rig.run(0.5, Intent())
        assert rig.cmd.last("zdistance")[2] == pytest.approx(0.0, abs=1e-9)

    def test_assisted_arrows_move_the_point_at_twelve_centimetres_a_second(self):
        rig = Rig()
        TestFlyTo.airborne(rig)
        previous = rig.cmd.last("position")
        fastest = 0.0
        for _ in range(round(1.0 / TICK_S)):
            TestFlyTo.follow(rig, TICK_S, Intent(forward=True))
            current = rig.cmd.last("position")
            fastest = max(fastest, math.hypot(current[1] - previous[1],
                                              current[2] - previous[2]) / TICK_S)
            previous = current
        assert fastest == pytest.approx(KEY_MOVE_SPEED_M_S, abs=1e-6)

    def test_a_mission_keeps_its_own_speed(self):
        rig = Rig()
        TestFlyTo.airborne(rig)
        rig.ctl.fly_to(SOMEWHERE.x + 2.0, SOMEWHERE.y, 0.40)
        previous = rig.cmd.last("position")
        fastest = 0.0
        for _ in range(round(4.0 / TICK_S)):
            TestFlyTo.follow(rig, TICK_S)
            current = rig.cmd.last("position")
            fastest = max(fastest, math.hypot(current[1] - previous[1],
                                              current[2] - previous[2]) / TICK_S)
            previous = current
        assert fastest == pytest.approx(MOVE_SPEED_M_S, abs=1e-6)
