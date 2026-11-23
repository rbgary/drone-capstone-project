# This file was modified on 11/4/2026 with the assistance of Claude Code in VS Code
# (Claude Opus 5.5, Anthropic, 2026). Positioning readiness no longer fails on a received
# base station that cannot be used yet (no calibration or stored place) when enough other
# stations are usable; readiness then rests on the 5 cm position-uncertainty check. Adds
# uncertainty_cm(), which shows that uncertainty rounded up to 0.1 cm in every message.

"""Is it safe to fly, and is it safe to keep flying?

Two questions, one module, both answered from the live telemetry stream.

**Before flight** — :func:`assess_positioning`. The lab crash of 2026-09-16 is
why this exists. Preflight counted ``lighthouse.bsAvailable`` (base stations the
drone holds *geometry* for: 4) and reported "4 base stations" while
``lighthouse.bsReceive`` — stations actually being received — was empty. The
drone flew on dead reckoning: at rest its x/y estimate wandered 8.9 m, and in
the air it flew sideways into the room. Readiness is now judged on reception,
per-station calibration and geometry, and the filter's own uncertainty.

**During flight** — :class:`FlightGuard`. The hover loop printed an estimate
that fell to −5.2 m below the floor and kept holding. Every sample is now
checked, and the answer is one of three:

- ``OK``   keep going
- ``LAND`` graceful landing — the estimate is still trustworthy enough to fly it
- ``STOP`` stop the motors — the estimate itself is gone, so a "landing" would
  steer by a position that does not exist. That is what flew the drone into
  the room. A short drop from hover height is the lesser harm.

The thresholds below are *safety policy*, not firmware facts, and are named so
they can be tuned from flight data. Where the firmware publishes its own
verdict (``pm.state`` low power, the supervisor's tumble flag) that is used.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from dronedeck.telemetry.stream import Snapshot

# ── positioning readiness ────────────────────────────────────────────────

# One base station is enough to FLY: Lighthouse V2 sweeps two angles from a
# single unit and the deck's four sensors resolve a pose from them. Measured
# 2026-09-22 in the cage — one station, drone at rest, position held to
# sub-millimetre with varPX at 0.0001 m² (1 cm).
#
# Two is still better, and what the kit was built around: a second station is
# a second viewpoint, so an occluded deck has somewhere else to look. With one,
# losing sight of it leaves x and y as the accelerometer integrating — measured
# the same day diverging at about 1 m/s, reaching 18 m in seconds. This drone
# has nothing to coast on: deck.bcFlow2 = 0 and deck.bcZRanger2 = 0, both read
# off the drone, so there is no optical flow and no rangefinder.
#
# The default is what the room actually has. A stricter room asks for two with
# DRONEDECK_MIN_STATIONS=2.
MIN_USABLE_STATIONS = 1

#: What the kit was designed around, and what the geometry pair-solver needs.
PREFERRED_STATIONS = 2


def required_stations() -> int:
    """How many base stations this room must have before positioning is ready.

    One by default — see MIN_USABLE_STATIONS for why that is enough to fly and
    what the second one buys. A room with two can insist on both with
    DRONEDECK_MIN_STATIONS=2, which is worth doing wherever they exist: the
    geometry pair-solver needs two, and only two give the drone somewhere else
    to look when its deck is occluded.

    ZERO IS NOT AN OPTION HERE, and not because of caution. With no station
    there is no position at all, so there is nothing for a position to be held
    against — the commanded point in flight/manual.py cannot be seeded, the
    geofence has nothing to measure and the drift guard has nothing to compare.
    Flying with no positioning is UNASSISTED flight, which is a different
    control law that the checks already offer by name. It is a different
    answer, not a looser threshold.
    """
    raw = os.environ.get("DRONEDECK_MIN_STATIONS", "").strip()
    if not raw:
        return MIN_USABLE_STATIONS
    try:
        wanted = int(raw)
    except ValueError:
        return MIN_USABLE_STATIONS
    return max(1, min(PREFERRED_STATIONS, wanted))
# Filter position variance, m². 0.0025 m² is a 5 cm standard deviation. The
# crash reading at rest was ~4.4 m².
MAX_READY_VARIANCE_M2 = 0.0025


def uncertainty_cm(variance_m2: float) -> float:
    """A variance as the standard deviation an operator reads, in cm, to 0.1 —
    ROUNDED UP (2026-11-04). Rounded to nearest, 5.04 cm read "5.0 cm (under 5
    needed)" and was refused, which looked like a reading under the limit being
    turned away. Rounded up, a refused reading always shows above 5.0. The
    inner round() keeps float noise (0.0004 ** 0.5 * 100 = 2.0000000000000004)
    from being rounded up to 2.1."""
    return math.ceil(round(math.sqrt(variance_m2) * 1000, 6)) / 10

# ── in-flight policy ─────────────────────────────────────────────────────

# Above this the estimate is too uncertain to LOCK a position to, even though
# the flight may continue. 0.01 m² is a 10 cm standard deviation. The drone
# publishes its own confidence, so manual position hold reads that rather than
# guessing from how far the number moved (flight/manual.py, flight/link.py).
# It sits between the 5 cm wanted at rest before arming and the 50 cm at which
# the estimate stops being a position at all.
MAX_HOLD_VARIANCE_M2 = 0.01

MAX_DRIFT_M = 0.30                  # sideways from the takeoff point, hold programs
MAX_HEIGHT_ERROR_M = 0.25           # from the target height, hold programs
HEIGHT_ERROR_GRACE_S = 1.0          # must persist this long (climb overshoot)
# Losing the beams is not the same as losing the position. The Kalman coasts
# on the IMU, and it says how well it is coasting: its variance GROWS while it
# has no measurement to correct with. So a blackout is ridden out for as long
# as the filter still stands behind its own answer, and ended early when it
# does not.
#
# Measured 2026-09-22: a 0.5 s dropout at 0.8 m landed a drone that was
# holding x and y to 1.7 cm, and the beams were back 0.2 s later. With one
# base station a brief occlusion is ORDINARY — the airframe's own tilt can
# break the line — so a fixed half-second was never going to survive it.
RECEPTION_LOST_GRACE_S = 0.5        # when the estimate is degrading too
RECEPTION_BLACKOUT_MAX_S = 3.0      # hard ceiling, however good it looks
#: Variance up to which a blacked-out estimate is still worth flying on.
#: Between MAX_HOLD_VARIANCE_M2 (0.01, tight enough to command a point) and
#: MAX_FLIGHT_VARIANCE_M2 (0.25, where it stops being a position at all).
RECEPTION_COAST_VARIANCE_M2 = 0.04  # 20 cm standard deviation
LOW_VOLTAGE_GRACE_S = 1.0           # a sag under a throttle step is not "empty"
# Telemetry is the DOWNLINK. Losing it means we are blind, not that the drone
# is in danger: the agent keeps sending setpoints at 50 Hz, and if the UPLINK
# is still alive the drone is still flying them. If the uplink is dead too,
# the firmware's own commander watchdog cuts within about half a second and no
# landing command of ours would have arrived anyway — so riding out a gap
# costs nothing in the case where it cannot help, and saves the flight in the
# case where it can.
#
# Measured 2026-09-22: a flight ended on "Lost contact with the drone's
# telemetry" with a drone that was otherwise flying well. There was no grace
# period at all — ONE late sample landed it, where every other guard here
# requires a fault to persist.
STALE_TELEMETRY_S = 0.5             # beyond this we are blind
STALE_TELEMETRY_GRACE_S = 1.5       # ...and this long before it ends a flight
# Beyond these the estimate is not a position — it is the accelerometer
# integrating. The crash trace fell ~1 m per sample at 4 Hz.
MAX_PLAUSIBLE_SPEED_M_S = 2.0
MAX_FLIGHT_VARIANCE_M2 = 0.25       # 50 cm standard deviation
BELOW_FLOOR_M = -0.30
ABOVE_CEILING_MARGIN_M = 0.50

CRITICAL_VBAT = 3.10                # measured floor, see flight/preflight.py

# `pm.state` values published by the firmware (pm.h: PMStates).
PM_LOW_POWER = 3
PM_SHUTDOWN = 4

# `supervisor.info` bit for "tumbled" (firmware supervisor.h). The firmware
# already cuts the motors when it sets this; the guard reports it.
SUPERVISOR_TUMBLED_BIT = 5

# ── crash cut-off ────────────────────────────────────────────────────────
#
# The firmware's tumble flag is not enough on its own. Measured 2026-09-30
# (trace_079af354): the drone flipped (roll 157.9 deg), then sat at pitch -38 to
# -85 deg with thrust pinned at 65535 for 24 s — the flag came only as the
# operator pressed Emergency stop. So the guard reads the attitude itself.
#
# PX4's failure detector disarms at 60 deg of roll or pitch held for 0.3 s
# (FD_FAIL_R, FD_FAIL_P, FD_FAIL_R_TTRI defaults). Tilt here is the angle from
# upright, acos(cos(roll) * cos(pitch)), so a diagonal lean counts in full.
# Against this drone's own record — 76 manual traces, 27,703 flying samples
# taken before any crash began — normal flight never passed 48.4 deg (p99.9
# 35.6 deg), while every one of the 50 crashes passed 60. Past 90 deg the drone
# is upside down: nothing is recovering it, so one sample is enough.
TILT_CUTOFF_DEG = 60.0
TILT_CUTOFF_S = 0.3
INVERTED_DEG = 90.0

# ── thrust at the ceiling ────────────────────────────────────────────────
#
# Thrust clipped at the motors' maximum means the controller is asking for more
# than the drone has, and attitude authority is what goes: with every motor at
# the limit there is nothing left to turn or level with. Hover measured at
# ~53,000 of 65,535 on this airframe — about 81 % — so there is little spare.
#
# NOT A CRASH PREDICTOR, and it is not sold as one: in the 76 traces, 35 of the
# 54 flights whose longest run at the ceiling was under 0.5 s crashed anyway —
# a flip is usually too fast for anything on this side of the radio. What a
# long run does say is that the drone is not holding itself up. Four flights
# sat at the ceiling for 2 s or more (3.0, 7.9, 16.2 and 16.7 s); the two
# longest flipped 0.1 s after the run ended, 14 s after this would have landed
# them. ArduCopter's thrust-loss check uses the same signal, throttle held at
# its limit, over 1 s; 2 s leaves out the 1.1 and 1.4 s runs of ordinary
# flight. Landing lowers the demand, which is the one thing that helps.
THRUST_CEILING = 65000                # stabilizer.thrust, clipped at 65535
THRUST_CEILING_S = 2.0


def tilt_deg(roll_deg: float, pitch_deg: float) -> float:
    """The angle between the drone's up and the world's up, degrees."""
    c = math.cos(math.radians(roll_deg)) * math.cos(math.radians(pitch_deg))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def station_ids(bitmask: float | None) -> tuple[int, ...]:
    if bitmask is None:
        return ()
    value = int(bitmask)
    return tuple(i for i in range(16) if value >> i & 1)


@dataclass(frozen=True)
class PositioningStatus:
    received: tuple[int, ...]
    calibrated: tuple[int, ...]
    with_geometry: tuple[int, ...]
    stored: tuple[int, ...]
    variance_m2: tuple[float | None, float | None, float | None]
    #: How many of the deck's four light sensors see light (lighthouse.width0-3);
    #: None when the stream does not carry them. 0 is not "no station decoded":
    #: no light reaches the deck at all — stations off, asleep, out of view,
    #: or a deck that is not receiving (2026-10-24: a whole session read 0).
    light_sensors: int | None = None

    @property
    def usable(self) -> tuple[int, ...]:
        """Received, and both calibration and geometry are valid."""
        return tuple(
            s for s in self.received if s in self.calibrated and s in self.with_geometry
        )

    @property
    def received_without_geometry(self) -> tuple[int, ...]:
        return tuple(s for s in self.received if s not in self.usable)

    @property
    def ready(self) -> bool:
        return not self.problems()

    def problems(self) -> list[str]:
        """Why positioning is not ready, in words an operator can act on."""
        out: list[str] = []
        if not self.received and self.light_sensors == 0:
            out.append(
                "No light reaches the positioning deck — all 4 of its sensors read 0. "
                "Check both base stations' front LEDs are solid green and can see the "
                "top of the drone, with nothing in between. If they are and it still "
                "reads 0 with the drone 1 m in front of a station, switch the drone "
                "off and on; if that does not help, reseat the positioning deck."
            )
        elif not self.received and self.light_sensors:
            out.append(
                f"Light reaches the positioning deck ({self.light_sensors} of 4 sensors), "
                "but no base station's sweep is decoded. Two stations on the same "
                "channel blur into none: give each its own — Ch 1 and Ch 2 (Set up, "
                "step 3)."
            )
        elif not self.received:
            out.append(
                "No base station signal is reaching the drone where it is now. "
                "Check the station is on (front LED solid green) and can see the "
                "top of the drone from where it sits: a station can reach a drone "
                "held up and miss it on the floor — raise it (about 1.5–2 m) and "
                "tilt it down at the spot, with nothing in between."
            )
        elif self.received_without_geometry and len(self.usable) < required_stations():
            # ONLY WHEN TOO FEW ARE USABLE (2026-11-04). The drone builds its
            # position from the stations it can use; one it has no calibration
            # or place for is ignored by its estimator, so it cannot make the
            # position worse. With enough usable stations, an extra one that is
            # merely in view (the room's second, not yet measured) no longer
            # blocks: the uncertainty check below decides. A room that insists
            # on two (DRONEDECK_MIN_STATIONS=2) is still told to measure it.
            #
            # Two different things are missing here and only one needs a person.
            # CALIBRATION is the station's own factory data, broadcast over the
            # air and picked up on its own after some seconds of clear line of
            # sight. GEOMETRY is where the station stands in THIS room, which
            # nothing broadcasts and which has to be measured once.
            #
            # This used to say "re-run geometry estimation in cfclient", naming
            # a tool this project replaced and does not install. An instruction
            # an operator cannot follow is worse than none.
            # Name the stage that is actually missing — not both at once: on
            # 2026-10-05 the calibration was already read and the operator was
            # told to wait for it AND that a station "may be blocked or off".
            waiting = [s for s in self.received_without_geometry if s not in self.calibrated]
            unmeasured = [s for s in self.received_without_geometry
                          if s in self.calibrated and s not in self.with_geometry]
            if unmeasured:
                ids = ", ".join(str(s) for s in unmeasured)
                out.append(
                    f"Base station {ids} is received and its sweeps decoded, but the "
                    f"drone does not know where the station stands, so it cannot turn "
                    f"them into a position. Measure it once in Control › Auto › ② Position: "
                    f"one press, the drone where it sits, no motors."
                )
            if waiting:
                ids = ", ".join(str(s) for s in waiting)
                out.append(
                    f"Base station {ids} is received; its calibration has not arrived "
                    f"yet. Give it a few seconds of clear line of sight."
                )
        wanted = required_stations()
        if self.received and not self.received_without_geometry and len(self.usable) < wanted:
            out.append(
                f"Only {len(self.usable)} usable base station(s); "
                f"{wanted} are needed. One may be blocked or off."
            )

        # The uncertainty is a SYMPTOM of everything above. Saying "keep the
        # drone still and wait" while the room has never been measured sends
        # the operator to wait for something that will never arrive — standing
        # still cannot produce geometry. So it is only said when there is
        # nothing better already said, and it never contradicts it.
        variances = [v for v in self.variance_m2 if v is not None]
        if len(variances) < 3:
            out.append("The position filter's uncertainty is not available.")
        elif max(variances) > MAX_READY_VARIANCE_M2:
            worst_cm = uncertainty_cm(max(variances))
            limit_cm = MAX_READY_VARIANCE_M2 ** 0.5 * 100
            if out:
                out.append(
                    f"Until then the position is uncertain by about {worst_cm:.1f} cm, "
                    f"where under {limit_cm:.0f} cm is needed."
                )
            else:
                out.append(
                    f"Position is uncertain by about {worst_cm:.1f} cm; it must be "
                    f"under {limit_cm:.0f} cm. Keep the drone still and wait for the "
                    f"base stations to lock on."
                )
        return out


def assess_positioning(snap: Snapshot) -> PositioningStatus:
    return PositioningStatus(
        received=station_ids(snap.get("lighthouse.bsReceive")),
        calibrated=station_ids(snap.get("lighthouse.bsCalVal")),
        with_geometry=station_ids(snap.get("lighthouse.bsGeoVal")),
        stored=station_ids(snap.get("lighthouse.bsAvailable")),
        variance_m2=(
            snap.get("kalman.varPX"),
            snap.get("kalman.varPY"),
            snap.get("kalman.varPZ"),
        ),
        light_sensors=_light_sensors(snap),
    )


def _light_sensors(snap: Snapshot) -> int | None:
    widths = [snap.get(f"lighthouse.width{i}") for i in range(4)]
    if all(w is None for w in widths):
        return None
    return sum(1 for w in widths if w)


def position_trusted(snap: Snapshot) -> bool:
    """Is the drone's x-y a position, or a number? A station the drone can USE
    (received, calibrated, measured) and the Kalman filter's own x-y spread
    within MAX_HOLD_VARIANCE_M2 — the bound at which manual flight stops
    moving its commanded point (flight/link.py _fix).

    Every screen that shows "where the drone is" asks this first. Without it
    a still drone with no station measured read as 92 m from the mission's
    start and climbing (2026-10-05): the estimate integrating the
    accelerometer, offered to the operator as a place.
    """
    status = assess_positioning(snap)
    if not status.usable:
        return False
    spread = [v for v in status.variance_m2[:2] if v is not None]
    return len(spread) == 2 and max(spread) <= MAX_HOLD_VARIANCE_M2


# ── in-flight guard ──────────────────────────────────────────────────────


class Action(StrEnum):
    OK = "ok"
    LAND = "land"
    STOP = "stop"


class Reason(StrEnum):
    NONE = "none"
    TUMBLED = "tumbled"
    THRUST_CEILING = "thrust_ceiling"
    POSITION_LOST = "position_lost"
    RECEPTION_LOST = "reception_lost"
    BATTERY_LOW = "battery_low"
    DRIFT = "drift"
    HEIGHT_ERROR = "height_error"
    OUTSIDE_FENCE = "outside_fence"
    TELEMETRY_STALE = "telemetry_stale"
    MOTORS_NOT_SPINNING = "motors_not_spinning"


@dataclass(frozen=True)
class Verdict:
    action: Action
    reason: Reason = Reason.NONE
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.action is Action.OK


OK = Verdict(Action.OK)


@dataclass(frozen=True)
class GuardContext:
    """What this flight is supposed to be doing, fixed at takeoff."""

    ground_z: float
    fence_half_extent_m: float
    max_height_m: float
    # Hold programs only. Manual flight moves on purpose, so drift and height
    # error do not apply there — reception, battery, fence and plausibility do.
    takeoff_xy: tuple[float, float] | None = None
    target_height_m: float | None = None
    #: False when the drone has no usable position estimate.
    #:
    #: Every position-derived guard then reads the accelerometer integrating,
    #: not a position: drift, fence, height error, plausibility and reception
    #: would all fire on noise, and "landing" would steer by a number that means
    #: nothing. They are switched off, deliberately and visibly, and the
    #: operator flying by eye is the guard. What still runs: tumble, battery,
    #: stale telemetry — and the heartbeat, which lands on a quiet window.
    assisted: bool = True
    #: A mission's room: whether (x, y) is inside its closed geofence
    #: (mission/plan/geofence.py). None keeps the square of
    #: `fence_half_extent_m`, which is what Manual and the hover demo fly in.
    fence: Callable[[float, float], bool] | None = None


class FlightGuard:
    """Checks each telemetry sample against the flight's context.

    Stateful only for the grace periods, so a single noisy sample never aborts
    a flight and a persistent fault always does.
    """

    def __init__(self, context: GuardContext) -> None:
        self.context = context
        self._reception_lost_since: float | None = None
        self._height_error_since: float | None = None
        self._low_voltage_since: float | None = None
        self._hold_armed_at: float | None = None
        self._tilted_since: float | None = None
        self._ceiling_since: float | None = None

    def arm_hold_checks(self, now: float) -> None:
        """Start drift/height checks — call once the takeoff climb completes."""
        self._hold_armed_at = now

    def _battery_verdict(self, snap: Snapshot, now: float) -> Verdict:
        """Low battery ends the operation, in both modes and both assist levels.

        The firmware's own `pm.state` is the authority; the voltage is a second
        opinion with a grace period, because a sag under a throttle step is not
        an empty battery.
        """
        pm_state = snap.get("pm.state")
        if pm_state is not None and int(pm_state) in (PM_LOW_POWER, PM_SHUTDOWN):
            return Verdict(
                Action.LAND, Reason.BATTERY_LOW,
                "The drone reports low battery — ending the operation and landing.",
            )
        vbat = snap.get("pm.vbat")
        if vbat is not None and vbat < CRITICAL_VBAT:
            if self._low_voltage_since is None:
                self._low_voltage_since = now
            if now - self._low_voltage_since > LOW_VOLTAGE_GRACE_S:
                return Verdict(
                    Action.LAND, Reason.BATTERY_LOW,
                    f"Battery at {vbat:.2f} V — ending the operation and landing.",
                )
        else:
            self._low_voltage_since = None
        return OK

    def _attitude_verdict(self, snap: Snapshot, now: float) -> Verdict:
        """Stop a drone that has gone over; land one the motors cannot hold."""
        roll, pitch = snap.get("stabilizer.roll"), snap.get("stabilizer.pitch")
        if roll is not None and pitch is not None:
            tilt = tilt_deg(roll, pitch)
            if tilt > TILT_CUTOFF_DEG:
                if self._tilted_since is None:
                    self._tilted_since = now
                if tilt > INVERTED_DEG or now - self._tilted_since >= TILT_CUTOFF_S:
                    return Verdict(
                        Action.STOP, Reason.TUMBLED,
                        f"The drone tipped {tilt:.0f}° from upright — motors stopped.",
                    )
            else:
                self._tilted_since = None

        thrust = snap.get("stabilizer.thrust")
        if thrust is not None and thrust >= THRUST_CEILING:
            if self._ceiling_since is None:
                self._ceiling_since = now
            if now - self._ceiling_since >= THRUST_CEILING_S:
                return Verdict(
                    Action.LAND, Reason.THRUST_CEILING,
                    f"The motors have been at full power for "
                    f"{now - self._ceiling_since:.1f} s and still cannot hold the drone — "
                    f"landing. A tired battery, a damaged propeller or too much weight "
                    f"on the drone does this.",
                )
        else:
            self._ceiling_since = None
        return OK

    def check(self, snap: Snapshot, now: float) -> Verdict:
        ctx = self.context
        # The snapshot's own age IS how long we have been blind, so no timer
        # is needed and a very old first sample lands at once rather than
        # starting a grace period from now.
        age = snap.age_s(now)
        if age is None or age > STALE_TELEMETRY_S + STALE_TELEMETRY_GRACE_S:
            seen = "never" if age is None else f"{age:.1f} s ago"
            return Verdict(
                Action.LAND, Reason.TELEMETRY_STALE,
                f"No telemetry from the drone ({seen}) — landing rather than "
                f"flying blind.",
            )
        if age > STALE_TELEMETRY_S:
            # Blind, but not yet lost. Every check below reads this snapshot,
            # and a stale one cannot say anything true about now — so the
            # honest answer is to hold the last setpoint and wait for a frame.
            return OK

        info = snap.get("supervisor.info")
        if info is not None and int(info) >> SUPERVISOR_TUMBLED_BIT & 1:
            return Verdict(
                Action.STOP, Reason.TUMBLED,
                "The drone reports it has tumbled — motors stopped.",
            )
        # Attitude and thrust come from the IMU and the controller, not from any
        # position, so these run assisted or not.
        verdict = self._attitude_verdict(snap, now)
        if not verdict.ok:
            return verdict

        # Everything below the battery reads the position estimate. Without base
        # stations there is no estimate to read, so those guards are off and the
        # operator flying by eye is the guard — the checks said so and the
        # operator accepted it. Battery and tumble still apply either way.
        if not ctx.assisted:
            return self._battery_verdict(snap, now)

        x = snap.get("stateEstimate.x")
        y = snap.get("stateEstimate.y")
        z = snap.get("stateEstimate.z")
        if x is None or y is None or z is None:
            return Verdict(Action.STOP, Reason.POSITION_LOST, "No position estimate.")
        height = z - ctx.ground_z

        speeds = [abs(v) for v in (snap.get("stateEstimate.vx"), snap.get("stateEstimate.vy"),
                                   snap.get("stateEstimate.vz")) if v is not None]
        variances = [v for v in (snap.get("kalman.varPX"), snap.get("kalman.varPY"))
                     if v is not None]
        if (
            (speeds and max(speeds) > MAX_PLAUSIBLE_SPEED_M_S)
            or height < BELOW_FLOOR_M
            or height > ctx.max_height_m + ABOVE_CEILING_MARGIN_M
            or (variances and max(variances) > MAX_FLIGHT_VARIANCE_M2)
        ):
            return Verdict(
                Action.STOP, Reason.POSITION_LOST,
                f"Position estimate is no longer believable (height {height:+.2f} m) — "
                f"motors stopped rather than steering by it.",
            )

        received = snap.get("lighthouse.bsReceive")
        if received is not None and int(received) == 0:
            if self._reception_lost_since is None:
                self._reception_lost_since = now
            blackout = now - self._reception_lost_since
            # The filter's variance is the honest signal: it grows while there
            # is nothing to correct with. While it is still tight the estimate
            # is worth flying on, whatever the beams are doing.
            coasting_well = bool(variances) and max(variances) <= RECEPTION_COAST_VARIANCE_M2
            limit = RECEPTION_BLACKOUT_MAX_S if coasting_well else RECEPTION_LOST_GRACE_S
            if blackout > limit:
                held = "and the estimate is going with it" if not coasting_well else (
                    f"for {blackout:.1f} s")
                return Verdict(
                    Action.LAND, Reason.RECEPTION_LOST,
                    f"Base station signal lost {held} — landing while the estimate "
                    f"still holds.",
                )
        else:
            self._reception_lost_since = None

        battery = self._battery_verdict(snap, now)
        if not battery.ok:
            return battery

        outside = (not ctx.fence(x, y) if ctx.fence is not None else
                   abs(x) > ctx.fence_half_extent_m or abs(y) > ctx.fence_half_extent_m)
        if outside:
            return Verdict(
                Action.LAND, Reason.OUTSIDE_FENCE,
                f"Outside the geofence at ({x:+.2f}, {y:+.2f}) m — landing.",
            )

        if self._hold_armed_at is not None and ctx.takeoff_xy is not None:
            dx, dy = x - ctx.takeoff_xy[0], y - ctx.takeoff_xy[1]
            drift = (dx * dx + dy * dy) ** 0.5
            if drift > MAX_DRIFT_M:
                return Verdict(
                    Action.LAND, Reason.DRIFT,
                    f"Drifted {drift * 100:.0f} cm from the takeoff point — landing.",
                )

        if self._hold_armed_at is not None and ctx.target_height_m is not None:
            if abs(height - ctx.target_height_m) > MAX_HEIGHT_ERROR_M:
                if self._height_error_since is None:
                    self._height_error_since = now
                if now - self._height_error_since > HEIGHT_ERROR_GRACE_S:
                    return Verdict(
                        Action.LAND, Reason.HEIGHT_ERROR,
                        f"Holding {height:.2f} m instead of {ctx.target_height_m:.2f} m — landing.",
                    )
            else:
                self._height_error_since = None

        return OK
