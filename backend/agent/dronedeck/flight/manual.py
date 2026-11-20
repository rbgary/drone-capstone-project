# This file was modified on 11/20/2026 with the assistance of Claude Code in VS Code
# (Claude Opus 5.5, Anthropic, 2026). Every graceful stop (landed, disarmed on the
# ground, shut down) now hands the setpoint priority back to the high-level commander
# after the stop setpoint, so the firmware's watchdog no longer locks the motors after
# a landing; the emergency stop still latches.

"""Manual control loop — assisted, gentle, and landed gracefully.

The rule this file exists to enforce is unchanged: **the operator sends intent;
the agent generates the setpoint stream.** A design that sends one packet per
keypress breaks the first time a packet is late, because the Crazyflie
commander stops accepting control below roughly 10 Hz and the drone drops. The
app reports which keys are held; this loop turns that into setpoints at 50 Hz
on its own, and lands if the app goes quiet.

What changed from the first version, and why:

- **Assisted, not raw thrust.** Up/down used to add 900 thrust per tick —
  about 45,000 a second, zero to full in one second — and releasing the keys
  bled thrust off so the drone sank. Keys now move a *target height* gently
  (:data:`CLIMB_RATE_M_S`), sent as a hover setpoint, and the drone's own
  position controller holds that height when nothing is pressed.
- **Armed idle.** After Start, the props turn gently on the ground
  (:data:`IDLE_THRUST`, just above cflib's "10001 = next to no power") so the
  operator can see the drone is live before it lifts.
- **Graceful landing, not a thrust fade.** Land hands over to the high-level
  commander's landing, which descends under position control. Emergency stop
  is separate and deliberate — the app requires holding a button for it.

**Two control laws, chosen by whether the drone knows where it is.**

  assisted    base stations usable. W and S move a target *height*; the drone
              holds it, and releasing every key HOLDS THE SPOT — see below.
  unassisted  no base stations. The height comes from the BAROMETER (the
              firmware's complementary estimator) and is held with a z-distance
              setpoint: W and S move a target height gently, letting go holds
              it, and Land lowers the target to the floor at a fixed rate. The
              arrows tilt the drone, because there is no position to move to.

**The keys move a POINT, not the drone.** Assisted flight used to send a
speed: hold an arrow, get 0.20 m/s. A speed says how fast and never says
where, so nothing in the system knew where the drone was supposed to be, and
sideways slip during a forward move was invisible to it. Releasing every key
sent a speed of zero, which asks the drone to stop — not to stay — and a drone
told to stop drifts, because every estimate error and every draught moves it
with nothing to bring it back.

So the agent now keeps ONE COMMANDED POINT — x, y, height and heading — and
sends it absolutely, every tick, moving or still. Each held key advances one
coordinate of that point at its own eased rate, for exactly as long as it is
held. The coordinates no key is asking for are never written to, so they stay
locked by construction rather than by a routine that watches for deviation:
that is why W climbs straight up instead of sliding, and why a forward press
travels forward and nothing else. Let go and the point simply stops advancing
— it is already the spot to hold, with no capture step and no gap.

Auto-correction is therefore not a mode that switches on. The point is the
only thing the drone is ever told, and the firmware's position controller
closes on it at 100 Hz — where an agent-side correction would be capped at
the radio's 10 Hz. There is deliberately no extra push past a threshold: an
absolute position is already the strongest correction this link can send, and
a deadband that ignores small error and then snaps at it is what lurches.

This is PX4's Position mode and ArduPilot's Loiter, which integrate stick
input into a position setpoint for the same reason. The leash below is
ArduPilot's too.

**Why a speed could never have worked.** A quadrotor is underactuated: it only
accelerates along its own thrust axis, so it moves sideways by tilting, and
any attitude error IS a horizontal acceleration error. One degree of unnoticed
tilt is g*sin(1 deg) = 0.17 m/s^2, which is 34 cm of drift in two seconds. A
velocity command leaves that error to integrate into position with nothing
observing position. Only closing the loop on position removes it.

**Multiple keys.** Each axis reads only its own opposite pair, so the axes
compose without interfering: Up+Right climbs while sliding right, Forward+Left
travels diagonally. An opposite pair held together cancels to zero, that
coordinate is not advanced, and it stays locked and actively held — pressing
Up and Down together really does mean "stay exactly here".

It needs a position to hold, so this is assisted flight only, and only while
the estimator stands behind its own numbers. Without one the loop falls back
to the old velocity law rather than inventing a coordinate to fly to.

The first unassisted law drove raw thrust. In the lab (2026-09-16) the slow
ramp through liftoff let the drone skid sideways on a leg and tumble, and
letting go of W did not hold anything. A barometer height is noisy — tens of
centimetres indoors — but it is a height, and the firmware can close the loop
on it where a person on a keyboard cannot.

Unassisted exists because the operator standing over the drone is allowed to
say it is fine to fly. It is offered only after the checks say assistance is
unavailable and the operator accepts that in as many words.

Deliberately independent of WebSockets and of cflib, so the safety behaviour
(heartbeat expiry, landing, stop) is testable without either.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from dronedeck.flight.keyframe import (
    FrameStatus,
    KeyFrame,
    keys_to_room,
    resolve,
    room_to_body,
)
from dronedeck.safety.flight_guard import MAX_PLAUSIBLE_SPEED_M_S

log = logging.getLogger(__name__)

TICK_HZ = 50.0
TICK_S = 1.0 / TICK_HZ

# The app is expected to ping ~10x/second. Half a second of silence is several
# missed pings, not a hiccup.
HEARTBEAT_TIMEOUT_S = 0.5

# cflib's send_setpoint docs: thrust 10001 is "next to no power". A little
# above that turns the props gently without lift — far below hover thrust.
IDLE_THRUST = 11000

CLIMB_RATE_M_S = 0.15               # gentle rise and fall while W / S are held
GOAL_REACHED_M = 0.01               # a held goal is done within a centimetre
LIFTOFF_HEIGHT_M = 0.05             # target above this means "flying"
TOUCHDOWN_HEIGHT_M = 0.05           # lowering below this lands
MAX_HEIGHT_M = 1.00
MOVE_SPEED_M_S = 0.20               # fly_to(): missions; mission time estimates use it
# The ARROW keys, assisted: slower than a mission (2026-09-30, the owner: "fly
# more steadily"). fly_to() keeps MOVE_SPEED_M_S.
KEY_MOVE_SPEED_M_S = 0.12
YAW_RATE_DEG_S = 30.0
LAND_S = 3.0

# ── glide ────────────────────────────────────────────────────────────────
#
# Nothing the keys command changes in a step. A key press used to set the climb
# speed instantly and a release stopped it instantly — a jolt at both ends that
# the height controller answered with the bounce seen in the lab traces
# (2026-09-17: motors slamming between 0 and full every 0.2–0.4 s). Every
# command now eases toward what the keys ask for at a bounded rate.
CLIMB_ACCEL_M_S2 = 0.30             # 0 → 0.15 m/s in 0.5 s; stops within ~4 cm
MOVE_ACCEL_M_S2 = 0.40              # assisted arrows: 0 → 0.20 m/s in 0.5 s
TILT_RATE_DEG_S = 8.0               # unassisted arrows: 0 → 2.5° in ~0.3 s
YAW_ACCEL_DEG_S2 = 90.0             # 0 → 30 °/s in ~0.3 s

# ── unassisted flight (barometer height hold) ────────────────────────────
#
# No base stations: the firmware estimates height from the barometer and holds
# a z-distance setpoint. Lower ceiling than assisted flight, because a baro
# height wanders by tens of centimetres and the operator has less margin.
MAX_UNASSISTED_HEIGHT_M = 0.80
# Arrows: HALF the old 5° (2026-09-30). With no position a lean is not a speed
# but an acceleration, g * tan(lean) — 0.43 m/s^2 here, 0.86 at 5° — for as
# long as it is held, and the drone coasts after: nothing measures its speed,
# so nothing can hold one. The lean is the only bound.
#
# A HELD ARROW LEANS FOR AS LONG AS IT IS HELD. For one build (2026-09-30) a
# press leaned for at most a second; in the lab the operator held ↓ for 2-5 s
# to bring back a drone drifting away in a small room, the lean faded to 0
# under the key (trace_7b36d994, 32:14: 3.4° -> 0.3° with ↓ still held), and
# the drone kept going: "the keys don't work anymore". Never cut a held key.
MAX_TILT_DEG = 2.5
# The generic z-distance packet and the legacy rpyt packet disagree on the sign
# of pitch (cfclient negates it for its height-hold mode). VERIFY IN THE LAB:
# if the up arrow moves the drone backwards, flip this.
PITCH_SIGN = -1.0
LAND_RATE_M_S = 0.25                # target lowered to the floor at this rate
TOUCHDOWN_HOLD_S = 0.6              # settle on the floor before the motors stop


class ControlState(StrEnum):
    IDLE = "idle"                   # disarmed, motors off
    ARMED = "armed"                 # props idling on the ground
    FLYING = "flying"
    LANDING = "landing"
    LANDED = "landed"               # down and disarmed; can arm again
    STOPPED = "stopped"             # emergency stop; needs a reset


class Commander(Protocol):
    """The subset of cflib's commander this loop needs."""

    def send_setpoint(self, roll: float, pitch: float, yaw_rate: float, thrust: int) -> None: ...

    def send_hover_setpoint(
        self, vx: float, vy: float, yawrate: float, zdistance: float
    ) -> None: ...

    def send_zdistance_setpoint(
        self, roll: float, pitch: float, yawrate: float, zdistance: float
    ) -> None: ...

    def send_position_setpoint(self, x: float, y: float, z: float, yaw: float) -> None: ...

    def send_notify_setpoint_stop(self, remain_valid_milliseconds: int = 0) -> None: ...

    def send_stop_setpoint(self) -> None: ...


@dataclass
class Intent:
    """What the operator is currently asking for.

    Held-key *state*, not an event stream. The app reports which controls are
    down; how often it reports has no bearing on how fast the drone is
    commanded.
    """

    up: bool = False
    down: bool = False
    forward: bool = False
    back: bool = False
    left: bool = False
    right: bool = False
    yaw_left: bool = False
    yaw_right: bool = False

    @classmethod
    def from_payload(cls, payload: dict) -> Intent:
        """Build from an app frame, ignoring anything unrecognised.

        Unknown keys are dropped rather than raising: a newer client sending an
        extra field must not knock the drone out of the air.
        """
        if not isinstance(payload, dict):
            raise ValueError("control frame must be an object")

        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: bool(v) for k, v in payload.items() if k in known})


@dataclass(frozen=True)
class Fix:
    """Where the drone says it is, in the estimator's own frame."""

    x: float
    y: float
    yaw_deg: float


#: How far the commanded point may get ahead of where the drone actually is.
#:
#: Holding a key advances the point whether or not the drone keeps up. If
#: something holds the drone back — wind, a snagged leg, a sagging battery —
#: an unleashed point would keep running and build an error the drone answers
#: with everything it has the moment it comes free. The leash caps that error,
#: so the worst lurch possible is a 30 cm one, and releasing the key always
#: settles within 30 cm. It also bounds the arrows to what the drone can
#: really fly: the point cannot outrun it.
#:
#: 0.30 m is well clear of the tracking lag at MOVE_SPEED_M_S (0.20 m/s), so
#: ordinary flight never touches it.
LEASH_M = 0.30

#: Drift off the commanded point that is worth saying out loud. Under this the
#: firmware is simply doing its job and there is nothing to report; over it,
#: something is winning against the position controller and the operator
#: should know before it becomes a geofence abort. The manual geofence is
#: +/-2.00 m, which is far too coarse to notice a hover sliding across a room.
DRIFT_NOTICE_M = 0.15


def _wrap_deg(degrees: float) -> float:
    """Fold a heading into [-180, 180)."""
    return (degrees + 180.0) % 360.0 - 180.0


@dataclass
class ManualStats:
    ticks: int = 0
    heartbeat_timeouts: int = 0
    emergency_stops: int = 0
    rejected_frames: int = 0


class ManualController:
    """Runs a 50 Hz control loop from the operator's held-key intent."""

    def __init__(
        self,
        commander: Commander,
        *,
        ground_z: float,
        land: Callable[[float, float], None],
        assisted: bool = True,
        position: Callable[[], Fix | None] | None = None,
        heading: Callable[[], float | None] | None = None,
        key_frame: KeyFrame = KeyFrame.OPERATOR,
        operator_xy: tuple[float, float] | None = None,
        on_frame_change: Callable[[FrameStatus | None], None] | None = None,
        heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S,
        tick_s: float = TICK_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        `ground_z` is the estimator z of the floor, captured by the checks.
        `land(z, duration_s)` is the high-level commander's landing.
        `assisted` is False when the drone has no usable position estimate: W
        and S then drive the throttle directly and the operator holds the
        height by eye, because there is no height for the drone to hold.
        `heading()` is the drone's heading now, degrees (stabilizer.yaw) —
        reported with or without base stations; it is what turns the arrows
        into the drone's own frame when no position can carry them.
        `key_frame` and `operator_xy` say which way the arrows move it
        (keyframe.py); `on_frame_change` hears whenever what they mean
        changes, outside the lock, so the app can say so.
        """
        self._commander = commander
        self._ground_z = ground_z
        self._land = land
        self._assisted = assisted
        #: Where the drone reports it is, for holding a spot. None when
        #: nothing is reporting one — then the loop keeps to velocities.
        self._position = position
        self._heading = heading
        #: Which way the arrows move the drone (keyframe.py). The operator's
        #: MARKED spot, if they said "I'm here"; otherwise the takeoff spot
        #: stands in for it, as ArduPilot's home is where the drone armed.
        self._key_frame = key_frame
        self._marked_operator = operator_xy
        self._on_frame_change = on_frame_change
        #: Where and which way this flight lifted off. Reset by arm().
        self._takeoff_xy: tuple[float, float] | None = None
        self._takeoff_heading: float | None = None
        #: What the arrows meant on the last flying tick; None on the ground.
        self._frame: FrameStatus | None = None
        self._frame_changed = False
        #: The point the drone is being told to be at: x, y and heading. The
        #: height is carried separately, in _target_height, because it is
        #: relative to the floor. None until a position has been reported.
        self._target: Fix | None = None
        #: How far the drone is from that point, metres. Read by the trace and
        #: by the app; 0 when no point is being commanded.
        self._drift_m = 0.0
        #: The last fix that was believed, with its timestamp — the jump test
        #: needs something to compare against.
        self._last_fix: tuple[float, Fix] | None = None
        self._touchdown_at: float | None = None
        # The eased commands. See "glide" above.
        self._climb_velocity = 0.0
        self._vx = 0.0
        self._vy = 0.0
        self._roll = 0.0
        self._pitch = 0.0
        self._yaw_rate = 0.0
        self._heartbeat_timeout = heartbeat_timeout_s
        self._tick_s = tick_s
        self._clock = clock

        self._lock = threading.Lock()
        self._intent = Intent()
        self._target_height = 0.0
        #: A height the agent is driving the target TOWARDS on the operator's
        #: behalf — "hold at 0.4 m" instead of holding W until it gets there.
        #: None whenever the keys are in charge, which is nearly always. See
        #: hold_at() for why this is a goal and not a jump.
        self._goal_height: float | None = None
        #: A point in the room the agent is gliding the commanded point towards
        #: — fly_to(). None whenever the keys are in charge. See fly_to().
        self._goal_xy: tuple[float, float] | None = None
        #: How fast the goal is approached: the mission's speed, never more
        #: than MOVE_SPEED_M_S. Set by fly_to().
        self._goal_speed = MOVE_SPEED_M_S
        #: Set when a held key cancelled a goal: the operator took the drone
        #: back. The mission controller reads it to stop commanding. Cleared by
        #: the next hold_at() or fly_to().
        self._operator_override = False
        self._last_heartbeat = clock()
        self._last_tick = clock()
        self._state = ControlState.IDLE
        self._landing_until: float | None = None
        self._landing_started = False
        self._thread: threading.Thread | None = None
        self._running = False
        self.stats = ManualStats()

    # ── state ────────────────────────────────────────────────────────────

    @property
    def state(self) -> ControlState:
        with self._lock:
            return self._state

    @property
    def target_height(self) -> float:
        with self._lock:
            return self._target_height

    @property
    def assisted(self) -> bool:
        return self._assisted

    @property
    def intent(self) -> Intent:
        """The keys currently held, for the flight trace."""
        with self._lock:
            return self._intent

    @property
    def target(self) -> Fix | None:
        """The point the drone is being told to be at, for the flight trace.

        It moves under the keys and stands still under none of them, so this
        is both "where it is going" and "the spot it is holding" — they were
        never two different things.
        """
        with self._lock:
            return self._target

    @property
    def drift_m(self) -> float:
        """How far the drone has been pushed off the spot it is holding.

        Zero while flying by key: there is no spot to be off. This is the
        number that says whether holding a spot is working.
        """
        with self._lock:
            return self._drift_m

    @property
    def climb_velocity(self) -> float:
        """The eased climb speed the height target is moving at, m/s."""
        with self._lock:
            return self._climb_velocity

    @property
    def ground_z(self) -> float:
        """The floor, in the estimator's frame. Barometric when unassisted."""
        return self._ground_z

    @property
    def goal_active(self) -> bool:
        """Whether the commanded point is still gliding towards a hold_at() or
        fly_to() goal. False once it has arrived, or a key cancelled it."""
        with self._lock:
            return self._goal_xy is not None or self._goal_height is not None

    @property
    def frame_status(self) -> FrameStatus | None:
        """What the arrow keys mean right now, while flying; None on the ground."""
        with self._lock:
            return self._frame

    @property
    def key_frame(self) -> KeyFrame:
        with self._lock:
            return self._key_frame

    @property
    def operator_override(self) -> bool:
        """A held key cancelled a goal: the operator has taken the drone back."""
        with self._lock:
            return self._operator_override

    # ── operator input ───────────────────────────────────────────────────

    def arm(self) -> None:
        """Start: props idle on the ground. Refused after an emergency stop."""
        with self._lock:
            if self._state is ControlState.STOPPED:
                raise RuntimeError("emergency stop is active — run the checks again first")
            if self._state not in (ControlState.IDLE, ControlState.LANDED):
                return
            self._intent = Intent()
            self._target_height = 0.0
            self._goal_height = None
            self._goal_xy = None
            self._operator_override = False
            self._reset_glide_locked()
            self._takeoff_xy = None
            self._takeoff_heading = None
            self._set_frame_locked(None)
            self._last_heartbeat = self._clock()
            self._last_tick = self._clock()
            self._state = ControlState.ARMED
        # The firmware locks thrust until it receives one setpoint with thrust
        # 0 (crtp_commander_rpyt). Without this every setpoint after it is
        # silently held at zero: the props never idle and the keys do nothing.
        # The lab's working scripts do the same (hop_test.py, keyboard_fly.py).
        self._commander.send_setpoint(0.0, 0.0, 0.0, 0)
        self._flush_frame_change()
        log.info("manual control: armed")

    def hold_at(self, height_m: float) -> None:
        """Rise to a height and hold it, without the operator holding W.

        THIS ADDS NO CONTROL LAW. It sets a goal that the EXISTING height glide
        drives towards at the same eased CLIMB_RATE_M_S the W key produces, and
        stops at the same ceiling. The drone is flown by exactly the code that
        already flies it; the only difference is what is asking for the climb.
        Everything else is untouched — the guards, the heartbeat dead-man, the
        leash, Land, and the position hold that happens when nothing is being
        asked for.

        IT IS A GOAL, NOT A JUMP. Writing `_target_height` directly would move
        the commanded height instantly, and the leash and the firmware's own
        controller would answer a step input with everything they have. The
        whole reason the glide exists (see "THE GLIDE" in manual-control.txt) is
        that a step at either end made the motors hammer.

        THE KEYS ALWAYS WIN. Any W or S cancels the goal on the next tick, so
        the operator can take over mid-climb without a mode to leave first.
        """
        with self._lock:
            if self._state not in (ControlState.ARMED, ControlState.FLYING):
                raise RuntimeError("the propellers are not running")
            ceiling = MAX_HEIGHT_M if self._assisted else MAX_UNASSISTED_HEIGHT_M
            if not 0.0 < height_m <= ceiling:
                raise ValueError(
                    f"hold height must be above 0 and at most {ceiling:.2f} m"
                )
            self._goal_height = height_m
            self._operator_override = False
        log.info("manual control: holding at %.2f m", height_m)

    def fly_to(self, x: float, y: float, height_m: float,
               speed_m_s: float | None = None) -> None:
        """Glide the commanded point to (x, y) in the room, at `height_m` above
        the floor, and hold it there.

        THIS ADDS NO CONTROL LAW, exactly like hold_at(). It sets a goal that the
        EXISTING motion drives towards: the same eased speed and acceleration
        the arrow keys produce (MOVE_SPEED_M_S, MOVE_ACCEL_M_S2), the same 30 cm
        leash, the same estimator gates, the same guards, the same trace. The
        mission controller flies a mission by calling this, one inspection
        point at a time — that is why autonomous flight inherits everything
        tuned into manual flight and nothing else.

        x and y are absolute Lighthouse metres, as the estimator reports them;
        the height is above the floor captured at takeoff (CLAUDE.md #4).

        IT ARRIVES, IT DOES NOT COAST PAST. The requested speed is capped at the
        speed the remaining distance can still be stopped from — the approach
        profile the height goal uses — so the point slows before the goal
        instead of overshooting it by the easing's stopping distance.

        THE KEYS ALWAYS WIN. Any held key cancels this goal (and a height goal)
        on the next tick and sets operator_override, so the operator can take
        the drone back mid-mission without a mode to leave.

        Assisted and airborne only: without a position there is nowhere to fly
        to, and on the ground there is no commanded point yet.

        `speed_m_s` (2026-10-01, Samuel: missions choose a slower, steadier
        speed): the goal is approached at it instead of MOVE_SPEED_M_S — never
        faster. None keeps MOVE_SPEED_M_S. It changes nothing the keys do.
        """
        if not all(math.isfinite(v) for v in (x, y, height_m)):
            raise ValueError("fly_to needs finite coordinates")
        speed = MOVE_SPEED_M_S if speed_m_s is None else speed_m_s
        if not (math.isfinite(speed) and 0.0 < speed <= MOVE_SPEED_M_S):
            raise ValueError(f"speed must be above 0 and at most {MOVE_SPEED_M_S:.2f} m/s")
        with self._lock:
            if not self._assisted:
                raise RuntimeError("the drone has no position estimate, so it cannot fly "
                                   "to a point")
            if self._state is not ControlState.FLYING:
                raise RuntimeError("the drone is not airborne")
            if self._target is None:
                raise RuntimeError("no position has been reported yet")
            if not 0.0 < height_m <= MAX_HEIGHT_M:
                raise ValueError(f"height must be above 0 and at most {MAX_HEIGHT_M:.2f} m")
            self._goal_xy = (x, y)
            self._goal_speed = speed
            self._goal_height = height_m
            self._operator_override = False
        log.info("manual control: flying to (%+.2f, %+.2f) at %.2f m, %.2f m/s",
                 x, y, height_m, speed)

    def set_intent(self, intent: Intent) -> None:
        with self._lock:
            self._intent = intent
            self._last_heartbeat = self._clock()

    def set_key_frame(self, key_frame: KeyFrame,
                      operator_xy: tuple[float, float] | None) -> None:
        """Which way the arrows move the drone, and the operator's marked spot
        (None: use where this flight took off). Takes effect on the next tick,
        in the air or on the ground."""
        with self._lock:
            self._key_frame = key_frame
            self._marked_operator = operator_xy
            if self._frame is not None:
                # Re-resolve at once so the app is told now, not a tick later.
                self._resolve_frame_locked(self._last_fix[1] if self._last_fix else None)
        self._flush_frame_change()
        log.info("manual control: arrows %s%s", key_frame,
                 "" if operator_xy is None else f", operator at ({operator_xy[0]:+.2f}, "
                 f"{operator_xy[1]:+.2f})")

    def set_frame_listener(self,
                           listener: Callable[[FrameStatus | None], None] | None) -> None:
        """Who hears when what the arrows mean changes (the session, for the
        app). Called outside the lock; a listener that raises is logged."""
        with self._lock:
            self._on_frame_change = listener

    def heartbeat(self) -> None:
        """Refresh liveness without changing what the operator is asking for."""
        with self._lock:
            self._last_heartbeat = self._clock()

    def land(self) -> None:
        """Graceful landing when airborne; disarm when only idling."""
        with self._lock:
            state = self._state
            if state is ControlState.FLYING:
                self._begin_landing_locked()
            elif state is ControlState.ARMED:
                self._state = ControlState.IDLE
        self._flush_frame_change()
        if state is ControlState.FLYING:
            return
        if state is ControlState.ARMED:
            self._stop_and_release()
            log.info("manual control: disarmed on the ground")

    def _stop_and_release(self) -> None:
        """Motors off, then hand the setpoint priority back — every graceful stop.

        A stop setpoint alone LOCKS the drone after a landing (supervisor.info
        580 two seconds after every mission landed, 2026-10-05 and 2026-11-20;
        firmware 2025.12.1 supervisor.c and supervisor_state_machine.c). The
        firmware counts the drone as flying for 2 s after any motor was above
        idle; the lone stop is the last setpoint it ever gets, so 0.5 s later
        the commander watchdog moves it to warning-level-out — which has no
        path to "landed" — and at 2 s to free fall, then LOCKED, cleared only
        by a restart. Relaxing the priority lets the stopped high-level
        commander write a fresh motors-off setpoint every tick, so the
        watchdog never fires and the supervisor goes flying → landed → ready.
        The same stop-then-notify the cflib examples end on.

        Not used by emergency_stop: that one is meant to latch, and Reset
        drone clears it over the radio.
        """
        self._commander.send_stop_setpoint()
        self._commander.send_notify_setpoint_stop()

    def emergency_stop(self) -> None:
        """Stop the motors now. Acts immediately, not on the next tick."""
        with self._lock:
            self._intent = Intent()
            self._goal_height = None
            self._goal_xy = None
            self._state = ControlState.STOPPED
            self.stats.emergency_stops += 1
            self._set_frame_locked(None)
        self._commander.send_stop_setpoint()
        self._flush_frame_change()
        log.warning("manual control: EMERGENCY STOP")

    def reset(self) -> None:
        """Clear an emergency stop. The session calls this only after re-checking."""
        with self._lock:
            if self._state is ControlState.STOPPED:
                self._state = ControlState.IDLE

    # ── loop ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="manual-control")
        self._thread.start()

    def stop(self) -> None:
        """Shut the loop down. Lands first if airborne — never just stops sending."""
        if self.state is ControlState.FLYING:
            self.land()
            landing_s = (LAND_S if self._assisted
                         else MAX_UNASSISTED_HEIGHT_M / LAND_RATE_M_S + TOUCHDOWN_HOLD_S)
            deadline = time.monotonic() + landing_s + 1.0
            while self.state is ControlState.LANDING and time.monotonic() < deadline:
                time.sleep(0.05)
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.state is not ControlState.LANDED:
            self._stop_and_release()

    def _run(self) -> None:
        """Tick on a fixed schedule, not a fixed sleep.

        Sleeping `tick_s` after each tick adds the tick's own cost and the OS
        timer slack to every period: measured at 38 Hz, not 50. Sleeping until
        the next deadline holds the rate, and resyncs rather than bursting if
        the loop falls a whole tick behind.
        """
        next_tick = time.monotonic()
        while self._running:
            self.tick()
            next_tick += self._tick_s
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -self._tick_s:
                next_tick = time.monotonic()

    def tick(self) -> None:
        """One control frame. Separated from the loop so tests can drive it."""
        send: Callable[[], None] | None = None
        with self._lock:
            now = self._clock()
            dt = min(max(now - self._last_tick, 0.0), 0.1)
            self._last_tick = now
            self.stats.ticks += 1
            state = self._state
            silent = now - self._last_heartbeat > self._heartbeat_timeout

            if state in (ControlState.ARMED, ControlState.FLYING) and not silent:
                self._keys_cancel_goals_locked(self._intent)

            if state is ControlState.ARMED:
                if silent:
                    self.stats.heartbeat_timeouts += 1
                    self._state = ControlState.IDLE
                    log.warning("manual control: app went quiet while armed — disarming")
                    send = self._stop_and_release
                else:
                    intent = self._intent
                    # Before liftoff only W lifts: S has nothing to lower yet.
                    self._glide_height_locked(
                        self._height_velocity_locked(intent, allow_down=False), dt)
                    if self._target_height > LIFTOFF_HEIGHT_M:
                        self._state = ControlState.FLYING
                        # The way the nose points as it lifts off is what the
                        # arrows keep when there is no position (keyframe.py).
                        self._takeoff_heading = self._heading_now_locked()
                        send = self._flight_setpoint_locked(intent, dt)
                    else:
                        send = lambda: self._commander.send_setpoint(0.0, 0.0, 0.0, IDLE_THRUST)  # noqa: E731

            elif state is ControlState.FLYING:
                if silent:
                    self.stats.heartbeat_timeouts += 1
                    log.warning("manual control: app went quiet — landing")
                    self._begin_landing_locked()
                else:
                    intent = self._intent
                    self._glide_height_locked(
                        self._height_velocity_locked(intent, allow_down=True), dt)
                    if intent.down and self._target_height <= TOUCHDOWN_HEIGHT_M:
                        self._begin_landing_locked()
                    else:
                        send = self._flight_setpoint_locked(intent, dt)

            if self._state is ControlState.LANDING:
                if self._assisted:
                    if self._landing_until is not None and now >= self._landing_until:
                        self._state = ControlState.LANDED
                        self._landing_until = None
                        send = self._stop_and_release
                    elif not self._landing_started:
                        self._landing_started = True
                        send = self._start_landing_commands
                else:
                    # Lower the target at a fixed rate — a controlled descent on
                    # the barometer, not a throttle cut — then settle and stop.
                    self._landing_started = True
                    self._glide_height_locked(-LAND_RATE_M_S, dt)
                    if self._target_height <= 0.0:
                        if self._touchdown_at is None:
                            self._touchdown_at = now
                        if now - self._touchdown_at >= TOUCHDOWN_HOLD_S:
                            self._state = ControlState.LANDED
                            self._touchdown_at = None
                            send = self._stop_and_release
                        else:
                            send = self._zdistance_setpoint_locked(Intent(), dt)
                    else:
                        send = self._zdistance_setpoint_locked(Intent(), dt)

        if send is not None:
            send()
        self._flush_frame_change()

    # ── the arrows' frame ────────────────────────────────────────────────

    def _heading_now_locked(self) -> float | None:
        """The drone's heading now, degrees, or None if nothing reports one."""
        if self._heading is None:
            return None
        try:
            heading = self._heading()
        except Exception:
            return None
        return None if heading is None or not math.isfinite(heading) else float(heading)

    def _resolve_frame_locked(self, fix: Fix | None) -> FrameStatus:
        """What the arrows mean this tick (keyframe.py), remembered for the app."""
        if self._marked_operator is not None:
            operator, source = self._marked_operator, "marked"
        elif self._takeoff_xy is not None:
            operator, source = self._takeoff_xy, "takeoff"
        else:
            operator, source = None, None
        status = resolve(
            self._key_frame,
            drone_xy=None if fix is None else (fix.x, fix.y),
            operator_xy=operator, operator_source=source,
            takeoff_heading_deg=self._takeoff_heading,
            was_near=self._frame.near if self._frame is not None else False,
        )
        self._set_frame_locked(status)
        return status

    def _set_frame_locked(self, status: FrameStatus | None) -> None:
        """Remember what the arrows mean; flag a change the app should hear.
        The operator direction turns continuously as the drone moves round,
        so only a change of frame, reason or spot is news — not the angle."""
        before = self._frame

        def news(s: FrameStatus | None) -> tuple | None:
            return None if s is None else (s.chosen, s.active, s.reason, s.operator,
                                           s.operator_source)

        if news(before) != news(status):
            self._frame_changed = True
        self._frame = status

    def _flush_frame_change(self) -> None:
        """Tell the listener what the arrows mean now, outside the lock. A
        broken listener must never stop the control loop."""
        with self._lock:
            if not self._frame_changed:
                return
            self._frame_changed = False
            status = self._frame
        if self._on_frame_change is None:
            return
        try:
            self._on_frame_change(status)
        except Exception:
            log.exception("manual control: the frame listener failed")

    # ── helpers (lock held) ──────────────────────────────────────────────

    @staticmethod
    def _ease(current: float, target: float, max_step: float) -> float:
        return current + max(-max_step, min(max_step, target - current))

    def _reset_glide_locked(self) -> None:
        self._climb_velocity = self._vx = self._vy = 0.0
        self._roll = self._pitch = self._yaw_rate = 0.0
        self._release_target_locked()

    def _keys_cancel_goals_locked(self, intent: Intent) -> None:
        """Any held key ends every goal, before anything moves this tick.

        One place, at the top of the tick, so no branch below can let a goal
        drive a coordinate the operator is already steering — the bug the
        height goal once had (see _height_velocity_locked).
        """
        if self._goal_xy is None and self._goal_height is None:
            return
        if any(dataclasses.astuple(intent)):
            self._goal_xy = None
            self._goal_height = None
            self._operator_override = True
            log.info("manual control: a key took over from the goal")

    def _goal_room_velocity_locked(self) -> tuple[float, float] | None:
        """The (x, y) speed the xy goal is asking for, in the room's frame —
        or None when there is no xy goal.

        It closes on the TARGET, never on the measured position: the target is
        what this controller owns, and the drone follows it on the leash
        (_goal_velocity_locked says why a second loop on position is wrong).
        """
        goal, target = self._goal_xy, self._target
        if goal is None or target is None:
            return None
        ex, ey = goal[0] - target.x, goal[1] - target.y
        distance = math.hypot(ex, ey)
        if distance <= GOAL_REACHED_M:
            # Arrived. The ordinary "no keys held" behaviour holds the spot from
            # here; there is no separate hold mode to leave.
            self._goal_xy = None
            return (0.0, 0.0)
        speed = min(self._goal_speed, math.sqrt(2.0 * MOVE_ACCEL_M_S2 * distance))
        # Already in the room's frame, the frame the arrows now move in too:
        # nothing to turn, whatever the heading.
        return (ex / distance * speed, ey / distance * speed)

    def _release_target_locked(self) -> None:
        self._target = None
        self._drift_m = 0.0
        self._last_fix = None

    def _believable_fix_locked(self) -> Fix | None:
        """The reported position, unless it moved faster than a drone can.

        A fix that jumps further between two ticks than the drone could
        possibly have flown is the ESTIMATOR moving, not the drone, and acting
        on it is the worst thing this loop can do: it would fly hard towards a
        place the drone already is. Measured in the lab on 2026-09-21 with
        stale base-station geometry — the estimate moved 21 cm in a single
        0.1 s step (2.1 m/s) while the drone sat still on the floor, with both
        stations received 96 % of the time. The arrows command 0.20 m/s.

        A rejected fix is not an error: the commanded point stays exactly
        where it was, and the drone keeps flying to it — which is the right
        answer if the estimate is the thing that moved.
        """
        if self._position is None:
            return None
        fix = self._position()
        if fix is None:
            self._last_fix = None
            return None
        now = self._clock()
        previous = self._last_fix
        if previous is not None:
            elapsed = now - previous[0]
            if elapsed > 0:
                moved = math.hypot(fix.x - previous[1].x, fix.y - previous[1].y)
                if moved / elapsed > MAX_PLAUSIBLE_SPEED_M_S:
                    log.warning(
                        "ignoring a position that jumped %.0f cm in %.0f ms — that is "
                        "the estimator, not the drone; check the base-station geometry",
                        moved * 100, elapsed * 1000,
                    )
                    return None
        self._last_fix = (now, fix)
        return fix

    def _height_velocity_locked(self, intent: Intent, allow_down: bool) -> float:
        """What moves the height target this tick — the keys, or the goal.

        ONE PLACE DECIDES, deliberately. The first version cancelled the goal
        inside the goal's own branch, so holding S took the other branch, never
        cancelled anything, and the goal resumed the moment the key was
        released — the drone climbed back to a height the operator had just
        flown away from. A test pins that now.
        """
        up = intent.up
        down = intent.down and allow_down
        if up or down:
            # THE KEYS ALWAYS WIN, and they win by cancelling rather than by
            # out-voting: two things moving one target is the bug above.
            self._goal_height = None
            return CLIMB_RATE_M_S * ((1 if up else 0) - (1 if down else 0))
        return self._goal_velocity_locked()

    def _goal_velocity_locked(self) -> float:
        """The climb the GOAL is asking for, or zero if nothing is.

        It closes on the TARGET, not on where the drone actually is. The target
        is the thing this controller owns; the drone follows it on the leash,
        and closing on a measured position here would be a second controller
        chasing the first at 50 Hz over a 10 Hz link — the arrangement that was
        deleted from this file once already (flight-tuning.txt).
        """
        goal = self._goal_height
        if goal is None:
            return 0.0
        error = goal - self._target_height
        if abs(error) <= GOAL_REACHED_M:
            # Arrived: let go, and the ordinary "no keys held" behaviour holds
            # the height from here. There is no separate hold mode to leave.
            self._goal_height = None
            return 0.0

        # SLOW DOWN BEFORE ARRIVING, not on arrival. The glide eases the climb
        # velocity out at CLIMB_ACCEL_M_S2, so a goal that asks for full climb
        # speed right up to the last centimetre still coasts the stopping
        # distance past it — v^2/2a = 0.15^2/0.6 = 3.7 cm, measured as a 2.6 cm
        # overshoot before this was added. Capping the request at the speed the
        # remaining distance can still be stopped from makes it arrive AT the
        # goal instead of near it, and it is the same profile the leash uses.
        approach = math.sqrt(2.0 * CLIMB_ACCEL_M_S2 * abs(error))
        speed = min(CLIMB_RATE_M_S, approach)
        return speed if error > 0 else -speed

    def _glide_height_locked(self, desired_velocity: float, dt: float) -> None:
        """Move the height target at an eased climb speed, within the ceiling."""
        ceiling = MAX_HEIGHT_M if self._assisted else MAX_UNASSISTED_HEIGHT_M
        self._climb_velocity = self._ease(
            self._climb_velocity, desired_velocity, CLIMB_ACCEL_M_S2 * dt)
        target = self._target_height + self._climb_velocity * dt
        if target >= ceiling or target <= 0.0:
            # Stopping at a limit is a hard edge by nature; the speed there is at
            # most the climb rate, which the height controller absorbs.
            self._climb_velocity = 0.0
        self._target_height = min(ceiling, max(0.0, target))

    def _flight_setpoint_locked(self, intent: Intent, dt: float) -> Callable[[], None]:
        if self._assisted:
            return self._hover_setpoint_locked(intent, dt)
        return self._zdistance_setpoint_locked(intent, dt)

    def _hover_setpoint_locked(self, intent: Intent, dt: float) -> Callable[[], None]:
        """The assisted law: hold a point, and let the keys move the point.

        Each held key advances ONE coordinate of the commanded point at its
        own eased rate. The coordinates no key is asking for are not touched,
        so they stay locked by construction — which is what makes W climb
        straight up instead of sliding, and what makes a forward press travel
        forward and nothing else. The point is then commanded absolutely,
        every tick, whether or not anything is held; the firmware flies to it
        at 100 Hz and corrects any difference on its own.

        THE ARROWS MOVE THE POINT IN THE ROOM, NEVER ALONG THE NOSE: away from
        the operator, or along the room's own directions (keyframe.py). The
        heading is only the nose — A and D turn it and nothing else.
        """
        forward = (1 if intent.forward else 0) - (1 if intent.back else 0)
        # cflib MotionCommander: left is +vy, turning left is +yawrate.
        left = (1 if intent.left else 0) - (1 if intent.right else 0)
        yaw = (1 if intent.yaw_left else 0) - (1 if intent.yaw_right else 0)

        fix = self._believable_fix_locked()
        if self._target is None and fix is not None:
            # Seeded from where the drone actually is, at liftoff — which is
            # also where "away from you" is measured from until the operator
            # marks their own spot.
            self._target = fix
            if self._takeoff_xy is None:
                self._takeoff_xy = (fix.x, fix.y)
            if self._takeoff_heading is None:
                self._takeoff_heading = fix.yaw_deg
        # Where the drone is, for "away from you". A fix the jump test refused
        # leaves the commanded point, which the drone is leashed to: the frame
        # must not flip because one reading was thrown away.
        where = fix if fix is not None else self._target
        frame = self._resolve_frame_locked(where)

        # A fly_to() goal asks for a speed exactly as a held arrow does; with
        # no goal (nearly always, in Manual) the keys ask. Never both: a key
        # cancelled the goal at the top of the tick. Both are ROOM velocities,
        # except with no heading at all, where the keys can only be the nose's.
        move = KEY_MOVE_SPEED_M_S
        if frame.forward_deg is None:
            keys = (forward * move, left * move)
        else:
            keys = keys_to_room(forward * move, left * move, frame.forward_deg)
        wanted = self._goal_room_velocity_locked() or keys
        self._vx = self._ease(self._vx, wanted[0], MOVE_ACCEL_M_S2 * dt)
        self._vy = self._ease(self._vy, wanted[1], MOVE_ACCEL_M_S2 * dt)
        self._yaw_rate = self._ease(self._yaw_rate, yaw * YAW_RATE_DEG_S, YAW_ACCEL_DEG_S2 * dt)
        # _glide_height_locked has already moved the height for this tick.
        z = self._ground_z + self._target_height
        vx, vy, rate = self._vx, self._vy, self._yaw_rate

        if self._target is None:
            # Until a position is reported there is no point to hold, so the old
            # velocity law flies it — that is unassisted flight in all but
            # name, and it is the only honest thing to do without a position.
            # Its velocities are in the drone's frame: turn the room ones by
            # the heading now, so the arrows still ignore the nose.
            heading = self._heading_now_locked()
            if frame.forward_deg is not None and heading is not None:
                vx, vy = room_to_body(vx, vy, heading)
            return lambda: self._commander.send_hover_setpoint(vx, vy, rate, z)

        self._advance_target_locked(vx, vy, rate, dt)
        if fix is not None:
            self._leash_target_locked(fix)
            self._drift_m = math.hypot(fix.x - self._target.x, fix.y - self._target.y)
            if self._drift_m > DRIFT_NOTICE_M:
                log.warning(
                    "%.0f cm off the commanded point and still correcting",
                    self._drift_m * 100)

        target = self._target
        return lambda: self._commander.send_position_setpoint(
            target.x, target.y, z, target.yaw_deg)

    def _advance_target_locked(self, vx: float, vy: float, yaw_rate: float, dt: float) -> None:
        """Move the commanded point by what the keys asked for this tick.

        (vx, vy) is already a ROOM velocity — keyframe.py turned the keys into
        the room's frame, whichever way the nose points — so it is added as it
        is. Yaw turns the nose and nothing else.

        (Until 2026-09-30 the keys were in the drone's own frame and this
        turned the step by the heading: "forward means where the nose
        points". The owner reversed that — see keyframe.py.)
        """
        target = self._target
        assert target is not None
        self._target = Fix(
            x=target.x + vx * dt,
            y=target.y + vy * dt,
            yaw_deg=_wrap_deg(target.yaw_deg + yaw_rate * dt),
        )

    def _leash_target_locked(self, fix: Fix) -> None:
        """Keep the commanded point within LEASH_M of the drone.

        Without this the point runs on at the key's speed while a held-back
        drone falls behind, and the error it is eventually asked to close is
        unbounded. Pulling the point in keeps the correction gentle and stops
        the arrows commanding more than the drone can actually fly.
        """
        target = self._target
        assert target is not None
        gap = math.hypot(target.x - fix.x, target.y - fix.y)
        if gap <= LEASH_M:
            return
        held_back = LEASH_M / gap
        self._target = Fix(
            x=fix.x + (target.x - fix.x) * held_back,
            y=fix.y + (target.y - fix.y) * held_back,
            yaw_deg=target.yaw_deg,
        )

    def _zdistance_setpoint_locked(self, intent: Intent, dt: float) -> Callable[[], None]:
        """Level attitude from the arrows, height held by the firmware on the
        barometer — the unassisted law. Tilt and yaw ease in and out.

        No base stations, so no room and no operator to be away from: the
        arrows keep the direction the nose had at takeoff (keyframe.py,
        "takeoff"), turned into the drone's frame by its heading now, so
        turning it with A and D does not turn the arrows. With no heading
        reported at all they tilt along the nose, as they always did."""
        forward = float((1 if intent.forward else 0) - (1 if intent.back else 0))
        left = float((1 if intent.left else 0) - (1 if intent.right else 0))
        # Turning left is a positive yaw rate (cflib MotionCommander.start_turn_left).
        yaw = (1 if intent.yaw_left else 0) - (1 if intent.yaw_right else 0)
        # Landing reuses this law with no keys held: nothing to turn, and the
        # arrows are not the operator's any more (the frame stays cleared).
        frame = self._resolve_frame_locked(None) if self._state is ControlState.FLYING else None
        heading = self._heading_now_locked()
        if frame is not None and frame.forward_deg is not None and heading is not None:
            forward, left = room_to_body(*keys_to_room(forward, left, frame.forward_deg), heading)
        self._pitch = self._ease(
            self._pitch, forward * MAX_TILT_DEG * PITCH_SIGN, TILT_RATE_DEG_S * dt)
        # Positive roll is to the right in the firmware's setpoint frame.
        self._roll = self._ease(self._roll, -left * MAX_TILT_DEG, TILT_RATE_DEG_S * dt)
        self._yaw_rate = self._ease(self._yaw_rate, yaw * YAW_RATE_DEG_S, YAW_ACCEL_DEG_S2 * dt)
        z = self._ground_z + self._target_height
        roll, pitch, rate = self._roll, self._pitch, self._yaw_rate
        return lambda: self._commander.send_zdistance_setpoint(roll, pitch, rate, z)

    def _begin_landing_locked(self) -> None:
        self._intent = Intent()
        # Coming down: the arrows no longer move it, so they mean nothing.
        self._set_frame_locked(None)
        # Coming down outranks any goal that was still climbing or travelling.
        self._goal_height = None
        self._goal_xy = None
        self._state = ControlState.LANDING
        self._landing_started = False
        self._landing_until = self._clock() + LAND_S
        self._touchdown_at = None

    def _start_landing_commands(self) -> None:
        # Low-level setpoints hold priority over the high-level commander
        # until released (CLAUDE.md #2). Without this the landing command is
        # ignored and the drone keeps hovering at its last setpoint.
        self._commander.send_notify_setpoint_stop()
        self._land(self._ground_z, LAND_S)
        log.info("manual control: landing")
