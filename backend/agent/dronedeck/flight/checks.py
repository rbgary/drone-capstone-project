"""The checks an operator watches before any flight, one step at a time.

This is preflight for a *session*: it runs on the session's single connection
and single telemetry stream, and it reports each step as it happens so the
desktop app can show a live checklist instead of a spinner followed by a
verdict.

Two holes in the original preflight let the lab crash of 2026-09-16 through,
and both are closed here:

1. It counted base stations with stored geometry, not stations being received.
   Positioning is now judged by :func:`assess_positioning`.
2. It waited for **z** to settle and never looked at x or y — which wandered
   8.9 m at rest with no base station signal. Settling now covers all three
   axes.

**A positioning problem warns; it does not refuse.** A base station that is off
is a recommendation, because the person standing over the drone can see it and
decide. What a warning changes is what the drone is trusted to do: the report
comes back ``assisted=False``, which costs the height hold, the preset programs
and every guard that reads a position. Only the two things the drone itself
refuses — no telemetry, ``sys.canfly``, and a supervisor holding the motors
after a crash — still stop a session outright.

The governing rule is unchanged: where the drone publishes its own verdict
(``sys.canfly``), read it; never re-derive it from an invented threshold.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dronedeck.flight import supervisor
from dronedeck.flight.preflight import (
    SETTLE_TIMEOUT_S,
    SETTLE_TOLERANCE_M,
    SETTLE_WINDOW,
    USABLE_FRACTION,
    VBAT_EMPTY,
    VBAT_FULL,
    estimate_endurance_s,
)
from dronedeck.safety.flight_guard import (
    PM_LOW_POWER,
    PM_SHUTDOWN,
    PositioningStatus,
    assess_positioning,
)
from dronedeck.telemetry.stream import Snapshot

log = logging.getLogger(__name__)

POSITIONING_TIMEOUT_S = 10.0
TELEMETRY_TIMEOUT_S = 5.0
SETTLE_POLL_S = 0.15

#: What to do when the drone does not detect its Lighthouse deck. Deck
#: detection reads a small memory chip on each deck over ONE shared pin, once,
#: at power-on (flight-log.txt, 2026-09-23): a deck that is fitted but not
#: detected is a contact fault on that pin, so the fix is in the operator's
#: hands and a power cycle proves it. The base stations are not the problem.
NO_LIGHTHOUSE_DECK = (
    "The drone does not detect its Lighthouse deck, so it cannot see the base "
    "stations however they are set up — the stations are not the problem. Switch "
    "the drone off, press the Lighthouse deck (the one with four light sensors, "
    "on top) firmly down onto its pins, and switch it on again: decks are only "
    "detected at power-on. Until then only unassisted manual flight is available, "
    "and the drone will drift."
)


class CheckKey(StrEnum):
    IDENTITY = "identity"
    TELEMETRY = "telemetry"
    MOTORS = "motors"
    BATTERY = "battery"
    DECK = "deck"
    POSITIONING = "positioning"
    ESTIMATE = "estimate"


LABELS: dict[CheckKey, str] = {
    CheckKey.IDENTITY: "Drone identified",
    CheckKey.TELEMETRY: "Live telemetry",
    CheckKey.MOTORS: "Motors unlocked",
    CheckKey.BATTERY: "Battery",
    CheckKey.DECK: "Positioning deck",
    CheckKey.POSITIONING: "Base stations",
    CheckKey.ESTIMATE: "Position settled",
}


class CheckStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    WARNING = "warning"             # flying is allowed, with less help from the drone
    FAILED = "failed"               # flying is not possible


@dataclass(frozen=True)
class CheckResult:
    key: CheckKey
    status: CheckStatus
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return LABELS[self.key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": str(self.key), "label": self.label, "status": str(self.status),
            "detail": self.detail, "data": self.data,
        }


@dataclass(frozen=True)
class ReadyReport:
    """Everything a flight needs from the checks, captured once they pass."""

    hardware_id: str
    vbat: float
    endurance_s: float
    ground_z_m: float
    takeoff_xy: tuple[float, float]
    estimate_spread_m: float
    positioning: PositioningStatus
    #: Whether the drone can hold a position and a height by itself.
    #:
    #: False means the base stations are not reaching it, or its estimate never
    #: settled. Flying is still allowed — the operator standing over the drone
    #: makes that call — but the drone cannot hold height, cannot return to a
    #: point, and the guards that watch drift and altitude cannot run. Only
    #: unassisted manual flight is offered in that state.
    assisted: bool = True
    #: Whether the drone detected its Lighthouse deck (deck.bcLighthouse4).
    #: False outranks every other reason: with no deck, no base station can
    #: reach the estimator however well the stations are working.
    lighthouse_deck: bool = True

    @property
    def unassisted_reason(self) -> str | None:
        """Why assistance is unavailable, in words, or None when it is fine."""
        if self.assisted:
            return None
        if not self.lighthouse_deck:
            # Named first and alone. The positioning problems would say "no base
            # station signal — check the stations are on", which is true and
            # useless: from 2026-09-23 to 09-30 every session said it, the
            # stations sat solid green, and the deck was the fault all along.
            return NO_LIGHTHOUSE_DECK
        return " ".join(self.positioning.problems()) or "The position estimate is not usable."

    def budget_s(self) -> float:
        """Seconds of flight this charge supports, with the reserve kept back."""
        return self.endurance_s * USABLE_FRACTION


def refusal_reason(vbat: float, pm_state: float | None, info: float | None) -> str:
    """Why the firmware will not fly, in the firmware's own terms.

    ``sys.canfly = 0`` is a VERDICT, not a reason. Reading it as "flat
    battery" sent an operator to the charger on 2026-09-22 holding a cell at
    4.15 V — nearly full — while the real answer sat in ``supervisor.info``
    the whole time: a latched crash, which a recovery request clears in under
    a second. The voltage is named only when the firmware itself says the
    power is the problem.

    The same rule as reading ``sys.canfly`` rather than inventing a voltage
    threshold, applied one level down: do not guess WHY the drone said no
    either.
    """
    bits = int(info) if info is not None else 0
    power = int(pm_state) if pm_state is not None else None

    if bits & supervisor.IS_TUMBLED:
        return (
            "The drone is not upright, so the firmware will not arm it "
            "(supervisor says tumbled). Stand it flat on the floor, the right "
            "way up, and run the checks again."
        )
    if bits & supervisor.IS_LOCKED:
        return (
            "The firmware has locked the motors after a crash. Stand the drone "
            "upright and run the checks again — they ask it to recover. If that "
            "does not clear it, power-cycle the drone. Not a battery problem."
        )
    if bits & supervisor.IS_CRASHED:
        return (
            "The firmware has latched a crash and will not arm until it is "
            "cleared. Stand the drone upright and run the checks again — they "
            "ask for recovery. NOT A BATTERY PROBLEM."
        )
    if power in (PM_LOW_POWER, PM_SHUTDOWN):
        return (
            f"The firmware reports the power is too low to fly at {vbat:.2f} V "
            f"(pm.state={power}). Charge the battery or swap in a charged one, "
            f"then power-cycle the drone."
        )
    return (
        f"The drone will not arm (sys.canfly=0) and has not said why: battery "
        f"{vbat:.2f} V, pm.state={power}, supervisor.info={bits} "
        f"(0b{bits:012b}). The battery reads healthy, so charging it is "
        f"probably not the answer — power-cycle the drone and try again."
    )


def battery_percent(vbat: float) -> int:
    """Approximate charge, for display only — never a gate (CLAUDE.md #1)."""
    frac = (vbat - VBAT_EMPTY) / (VBAT_FULL - VBAT_EMPTY)
    return round(max(0.0, min(1.0, frac)) * 100)


def read_hardware_id(cf: Any) -> str:
    """The drone's own unique ID, from its MCU (``cpu.id0..2``).

    Not the radio address: two Crazyflies can share ``radio://0/80/2M``, and
    tagging flights by address would mix their data. Confirmed present on the
    lab drone's firmware.
    """
    parts = [int(cf.param.get_value(f"cpu.id{i}")) for i in range(3)]
    return "cf-" + "".join(f"{p & 0xFFFFFFFF:08x}" for p in parts)


class ChecksFailed(RuntimeError):
    def __init__(self, result: CheckResult) -> None:
        super().__init__(f"{result.key}: {result.detail}")
        self.result = result


def run_checks(
    cf: Any,
    snapshot: Callable[[], Snapshot],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    reset_estimator: Callable[[], None] | None = None,
    request_recovery: Callable[[], None] | None = None,
) -> Generator[CheckResult, None, ReadyReport]:
    """Yield each check's progress. Raises :class:`ChecksFailed` on the first
    failure, after yielding it. Returns a :class:`ReadyReport` via
    ``StopIteration.value`` — use :func:`collect` to get it.

    ``request_recovery`` asks the firmware to clear a crash. Without it a
    crashed drone fails the motors check instead of being recovered.
    """

    def running(key: CheckKey, detail: str = "") -> CheckResult:
        return CheckResult(key, CheckStatus.RUNNING, detail)

    def fail(key: CheckKey, detail: str, **data: Any) -> CheckResult:
        return CheckResult(key, CheckStatus.FAILED, detail, data)

    def warn(key: CheckKey, detail: str, **data: Any) -> CheckResult:
        return CheckResult(key, CheckStatus.WARNING, detail, data)

    def last_estimate() -> tuple[float, float, float]:
        """Whatever the drone last reported, for the record. Not a position.

        With no base stations these are the accelerometer integrating, so they
        are stored so the flight has *a* reference and used for nothing that
        steers the drone.
        """
        s = snapshot()
        return (s.get("stateEstimate.x") or 0.0,
                s.get("stateEstimate.y") or 0.0,
                s.get("stateEstimate.z") or 0.0)

    # 1. Identity — ask the drone who it is.
    yield running(CheckKey.IDENTITY)
    try:
        hardware_id = read_hardware_id(cf)
    except Exception as e:
        result = fail(CheckKey.IDENTITY, f"Could not read the drone's ID ({type(e).__name__}).")
        yield result
        raise ChecksFailed(result) from e
    yield CheckResult(
        CheckKey.IDENTITY, CheckStatus.PASSED, hardware_id, {"hardware_id": hardware_id}
    )

    # 2. Telemetry actually flowing — every later check reads it.
    yield running(CheckKey.TELEMETRY)
    deadline = clock() + TELEMETRY_TIMEOUT_S
    snap = snapshot()
    while snap.updated_at is None or (snap.age_s(clock()) or 0) > 1.0:
        if clock() > deadline:
            result = fail(CheckKey.TELEMETRY, "No telemetry from the drone. Check the radio link.")
            yield result
            raise ChecksFailed(result)
        sleep(0.1)
        snap = snapshot()
    yield CheckResult(CheckKey.TELEMETRY, CheckStatus.PASSED, "Receiving at 10 Hz")

    # 3. Motors — is the firmware holding them after a crash? Recover if that is
    #    all it is. A retry runs exactly this, so a crashed drone is never armed
    #    into a flight whose motors cannot spin (flight/supervisor.py).
    yield running(CheckKey.MOTORS, "Asking the drone")

    def read_bits() -> int | None:
        value = snapshot().get("supervisor.info")
        return None if value is None else int(value)

    motors = supervisor.ensure_motors_unlocked(
        read_bits, request_recovery, clock=clock, sleep=sleep
    )
    # The named bits go in the record, not just the verdict. A verdict with no
    # reading behind it is what reported a latched crash as a flat battery.
    motor_data = {"state": str(motors.state), "bits": motors.bits,
                  "supervisor": list(motors.names)}
    if not motors.ok:
        result = fail(CheckKey.MOTORS, motors.message, **motor_data)
        yield result
        raise ChecksFailed(result)
    softened = (supervisor.MotorState.UNKNOWN, supervisor.MotorState.STILL_FLYING)
    yield CheckResult(
        CheckKey.MOTORS,
        CheckStatus.WARNING if motors.state in softened else CheckStatus.PASSED,
        motors.message, motor_data,
    )

    # 4. Battery — the firmware's verdict, plus endurance for information.
    #
    # Read afresh, and waited for. `snap` above is the FIRST sample that
    # arrived, and the stream comes in five log blocks that land separately: it
    # can hold one block and not the one carrying pm.vbat and sys.canfly.
    # Measured 2026-09-30 — `dronedeck check` failed "Battery readings are
    # not available" against a drone reporting 3.80 V a second later.
    yield running(CheckKey.BATTERY)
    deadline = clock() + TELEMETRY_TIMEOUT_S
    snap = snapshot()
    while (snap.get("pm.vbat") is None or snap.get("sys.canfly") is None) and clock() < deadline:
        sleep(0.1)
        snap = snapshot()
    vbat, canfly = snap.get("pm.vbat"), snap.get("sys.canfly")
    if vbat is None or canfly is None:
        result = fail(CheckKey.BATTERY, "Battery readings are not available.")
        yield result
        raise ChecksFailed(result)
    endurance = estimate_endurance_s(vbat)
    battery_data = {"vbat": round(vbat, 2), "percent": battery_percent(vbat),
                    "canfly": bool(canfly), "endurance_s": round(endurance)}
    if not canfly:
        result = fail(
            CheckKey.BATTERY,
            refusal_reason(vbat, snap.get("pm.state"), snap.get("supervisor.info")),
            **battery_data,
        )
        yield result
        raise ChecksFailed(result)
    yield CheckResult(
        CheckKey.BATTERY, CheckStatus.PASSED,
        f"{vbat:.2f} V · about {battery_percent(vbat)}% · ~{endurance:.0f} s of hover",
        battery_data,
    )

    # 5. Positioning deck fitted — ask the drone, never infer (CLAUDE.md #5).
    #
    # From here on nothing blocks the flight. Positioning decides how much the
    # drone can do for itself, and the operator standing over it decides whether
    # to fly: a base station that is off is a recommendation, not a refusal.
    yield running(CheckKey.DECK)
    if str(cf.param.get_value("deck.bcLighthouse4")) != "1":
        # No deck means no positioning at all, whatever the lighthouse
        # variables happen to read — there is nothing left to wait for.
        yield warn(CheckKey.DECK, NO_LIGHTHOUSE_DECK)
        px, py, pz = last_estimate()
        return ReadyReport(
            hardware_id=hardware_id, vbat=vbat, endurance_s=endurance,
            ground_z_m=pz, takeoff_xy=(px, py), estimate_spread_m=float("inf"),
            positioning=assess_positioning(snapshot()), assisted=False,
            lighthouse_deck=False,
        )
    yield CheckResult(CheckKey.DECK, CheckStatus.PASSED, "Lighthouse deck fitted")

    # 6. Base stations received, calibrated, with geometry, and a confident filter.
    yield running(CheckKey.POSITIONING, "Waiting for base station signal")
    deadline = clock() + POSITIONING_TIMEOUT_S
    status = assess_positioning(snapshot())
    while not status.ready and clock() < deadline:
        sleep(0.25)
        status = assess_positioning(snapshot())
    positioning_data = {
        "received": list(status.received), "usable": list(status.usable),
        "stored": list(status.stored),
    }
    if not status.ready:
        yield warn(
            CheckKey.POSITIONING,
            " ".join(status.problems()) + " Manual flight is still available, "
            "unassisted — you control the height.",
            **positioning_data,
        )
        px, py, pz = last_estimate()
        return ReadyReport(
            hardware_id=hardware_id, vbat=vbat, endurance_s=endurance,
            ground_z_m=pz, takeoff_xy=(px, py), estimate_spread_m=float("inf"),
            positioning=status, assisted=False,
        )
    yield CheckResult(
        CheckKey.POSITIONING, CheckStatus.PASSED,
        f"Receiving base stations {', '.join(str(s) for s in status.usable)}",
        positioning_data,
    )

    # 7. Settled on all three axes, after a filter reset (CLAUDE.md #3).
    yield running(CheckKey.ESTIMATE, "Keep the drone still")
    if reset_estimator is not None:
        reset_estimator()
    window: list[tuple[float, float, float]] = []
    best = float("inf")
    deadline = clock() + SETTLE_TIMEOUT_S
    while clock() < deadline:
        s = snapshot()
        x, y, z = s.get("stateEstimate.x"), s.get("stateEstimate.y"), s.get("stateEstimate.z")
        if x is not None and y is not None and z is not None:
            window.append((x, y, z))
            window = window[-SETTLE_WINDOW:]
            if len(window) == SETTLE_WINDOW:
                spread = max(
                    max(p[a] for p in window) - min(p[a] for p in window) for a in range(3)
                )
                best = min(best, spread)
                if spread < SETTLE_TOLERANCE_M:
                    gx = sum(p[0] for p in window) / SETTLE_WINDOW
                    gy = sum(p[1] for p in window) / SETTLE_WINDOW
                    gz = sum(p[2] for p in window) / SETTLE_WINDOW
                    yield CheckResult(
                        CheckKey.ESTIMATE, CheckStatus.PASSED,
                        f"Steady to {spread * 100:.1f} cm",
                        {"spread_m": round(spread, 4), "ground_z_m": round(gz, 3)},
                    )
                    return ReadyReport(
                        hardware_id=hardware_id, vbat=vbat, endurance_s=endurance,
                        ground_z_m=gz, takeoff_xy=(gx, gy), estimate_spread_m=spread,
                        positioning=status,
                    )
        sleep(SETTLE_POLL_S)

    shown = "no readings" if best == float("inf") else f"{best * 100:.1f} cm"
    yield warn(
        CheckKey.ESTIMATE,
        f"The position never settled (best {shown}; it needs under "
        f"{SETTLE_TOLERANCE_M * 100:.0f} cm on every axis). The drone cannot hold a "
        f"height or a point, so only unassisted manual flight is available. Keep it "
        f"still, hands off, and check the base stations to get assistance back.",
        spread_m=None if best == float("inf") else round(best, 4),
    )
    px, py, pz = last_estimate()
    return ReadyReport(
        hardware_id=hardware_id, vbat=vbat, endurance_s=endurance,
        ground_z_m=pz, takeoff_xy=(px, py), estimate_spread_m=best,
        positioning=status, assisted=False,
    )


def collect(
    steps: Generator[CheckResult, None, ReadyReport], on_step: Callable[[CheckResult], None]
) -> ReadyReport:
    """Drive :func:`run_checks`, forwarding each step, and return the report."""
    while True:
        try:
            on_step(next(steps))
        except StopIteration as done:
            report: ReadyReport = done.value
            return report


# ── propeller test ───────────────────────────────────────────────────────

PROP_TEST_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class PropTestResult:
    passed: tuple[int, ...]         # motor numbers, 1–4
    failed: tuple[int, ...]

    @property
    def ok(self) -> bool:
        return not self.failed and len(self.passed) == 4

    def to_dict(self) -> dict[str, Any]:
        return {"passed": list(self.passed), "failed": list(self.failed), "ok": self.ok}


def run_prop_test(
    cf: Any,
    read_health: Callable[[], tuple[int, int]],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> PropTestResult:
    """The firmware's own propeller test: spins each motor briefly, reports each.

    ``read_health`` returns ``(health.motorTestCount, health.motorPass)``. The
    test is finished when the count increments; ``motorPass`` bit n is motor
    n+1. Both variables and the ``health.startPropTest`` parameter were
    confirmed on the lab drone's firmware.

    Spins the motors. The caller must already have the operator's confirmation
    that the drone is on the floor and clear.
    """
    count_before, _ = read_health()
    cf.param.set_value("health.startPropTest", "1")
    deadline = clock() + PROP_TEST_TIMEOUT_S
    while clock() < deadline:
        sleep(0.25)
        count, passed_bits = read_health()
        if count != count_before:
            passed = tuple(m + 1 for m in range(4) if passed_bits >> m & 1)
            failed = tuple(m + 1 for m in range(4) if not passed_bits >> m & 1)
            return PropTestResult(passed, failed)
    raise TimeoutError("the propeller test did not report a result")


# ── battery test ─────────────────────────────────────────────────────────
#
# The firmware's own (health.c): it records the resting voltage, runs all four
# motors briefly at ``health.batTestPWMRatio``, records the lowest loaded
# voltage, and publishes the difference as ``health.batterySag`` with its own
# pass/fail as ``health.batteryPass``. The threshold is the firmware's; nothing
# here invents a voltage (CLAUDE.md #1).
#
# The test does not report "finished" the way the propeller test does, and its
# load lasts well under a second — too short to catch reliably on a 10 Hz
# stream. So the result is read after a fixed wait that covers it, and the
# ratio the drone actually used is read and recorded beside it.

BATTERY_TEST_WAIT_S = 3.0


@dataclass(frozen=True)
class BatteryTestResult:
    sag_v: float
    passed: bool
    idle_vbat: float | None
    #: `health.batTestPWMRatio` as the drone reported it; 0 means the
    #: firmware's built-in default.
    pwm_ratio: float | None

    def to_dict(self) -> dict[str, Any]:
        return {"sag_v": round(self.sag_v, 3), "passed": self.passed,
                "idle_vbat": None if self.idle_vbat is None else round(self.idle_vbat, 2),
                "pwm_ratio": self.pwm_ratio}


def run_battery_test(
    cf: Any,
    read_result: Callable[[], tuple[float, int]],
    snapshot: Callable[[], Snapshot],
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> BatteryTestResult:
    """Spins all four motors briefly. The caller must already hold the
    operator's confirmation that the drone is on the floor and clear.

    ``read_result`` returns ``(health.batterySag, health.batteryPass)``.
    """
    try:
        ratio: float | None = float(cf.param.get_value("health.batTestPWMRatio"))
    except Exception:
        ratio = None
    idle = snapshot().get("pm.vbat")
    cf.param.set_value("health.startBatTest", "1")
    sleep(BATTERY_TEST_WAIT_S)
    sag, passed = read_result()
    return BatteryTestResult(sag_v=float(sag), passed=bool(passed), idle_vbat=idle, pwm_ratio=ratio)


@dataclass(frozen=True)
class HealthTestResult:
    """The motors one at a time, then the battery under all four."""

    motors: PropTestResult
    battery: BatteryTestResult | None
    #: Why the battery half has no result, when it has none.
    battery_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.motors.ok and self.battery is not None and self.battery.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "motors": self.motors.to_dict(),
            "battery": None if self.battery is None else self.battery.to_dict(),
            "battery_error": self.battery_error,
        }

