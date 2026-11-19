"""One connection to one drone — the only way anything in the agent flies.

Before this, five paths could move the drone: the CLI's ``hover``, ``goto`` and
``mission``, the ``poll`` loop, and an unauthenticated REST endpoint. All of
them used a flight object that slept through its waits without watching
anything. Now there is one path:

    DroneLink.open()            connect, configure, start the telemetry stream
      .checks()                 the live preflight checklist → ReadyReport
      .prop_test()              the firmware's propeller test
      .health_test()            propellers, then the battery under load
      .guarded_flight(report)   autonomous flight, every wait watched
      .manual(report)           the assisted manual controller
    DroneLink.close()           stop the stream, stop the motors, close the link

The desktop session and the CLI both use it, so the safety behaviour exercised
in the lab is the behaviour of every caller.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Generator
from typing import Any

from dronedeck.flight import core, preflight
from dronedeck.flight.checks import (
    BatteryTestResult,
    CheckResult,
    HealthTestResult,
    PropTestResult,
    ReadyReport,
    read_hardware_id,
    run_battery_test,
    run_checks,
    run_prop_test,
)
from dronedeck.flight.control import GuardedFlight, PhaseEvent
from dronedeck.flight.manual import Fix, ManualController

# Kept under link's own names: tests patch `link._nothing_found_reason`.
from dronedeck.flight.radio import nothing_found_reason as _nothing_found_reason
from dronedeck.flight.radio import unopenable_reason
from dronedeck.flight.tuning import BAROMETER_PROFILE, BASE_PROFILE, Applied, FlightTuning
from dronedeck.paths import cflib_cache_dir
from dronedeck.safety.flight_guard import (
    MAX_HOLD_VARIANCE_M2,
    FlightGuard,
    GuardContext,
)
from dronedeck.telemetry.reader import stream_variables_for
from dronedeck.telemetry.stream import Snapshot, TelemetryStream

log = logging.getLogger(__name__)

DEFAULT_FENCE_M = 2.0
DEFAULT_MAX_HEIGHT_M = 1.0

# `stabilizer.estimator`: 1 complementary (height from the barometer), 2 Kalman
# (height from the base stations). Switched at runtime for unassisted flight.
ESTIMATOR_PARAM = "stabilizer.estimator"
ESTIMATOR_COMPLEMENTARY = "1"
ESTIMATOR_KALMAN = "2"
ESTIMATOR_SETTLE_S = 1.5


class LinkError(RuntimeError):
    """The drone could not be reached. The message is safe to show an operator."""


def _default_scf(uri: str) -> Any:
    from cflib.crazyflie import Crazyflie
    from cflib.crazyflie.syncCrazyflie import SyncCrazyflie

    return SyncCrazyflie(uri, cf=Crazyflie(rw_cache=str(cflib_cache_dir())))


def _default_stream(scf: Any) -> TelemetryStream:
    return TelemetryStream(scf, variables=stream_variables_for(scf))


def _default_scan() -> list[str]:
    import cflib.crtp

    cflib.crtp.init_drivers()
    return [found[0] for found in cflib.crtp.scan_interfaces()]



def _fix(snapshot: Snapshot) -> Fix | None:
    """Where the drone believes it is, and how much it believes it.

    None whenever the estimator has not reported all three numbers — a
    half-filled fix is worse than no fix, because the missing one reads as
    x=0, which is the middle of the room.

    Also None when the Kalman filter's own variance says the estimate is
    loose (MAX_HOLD_VARIANCE_M2, a 10 cm standard deviation). The drone
    publishes its confidence, so this reads it instead of inferring one:
    a guessed threshold refused flights the firmware would have allowed, and
    the same mistake in reverse would lock a position to a number the
    estimator itself does not stand behind. The flight continues either way
    — the guard has its own, looser bound — but the commanded point stops
    being moved by a reading that cannot carry it.
    """
    x = snapshot.get("stateEstimate.x")
    y = snapshot.get("stateEstimate.y")
    yaw = snapshot.get("stabilizer.yaw")
    if x is None or y is None or yaw is None:
        return None
    spread = [v for v in (snapshot.get("kalman.varPX"), snapshot.get("kalman.varPY"))
              if v is not None]
    if spread and max(spread) > MAX_HOLD_VARIANCE_M2:
        return None
    return Fix(float(x), float(y), float(yaw))


def _heading(snapshot: Snapshot) -> float | None:
    """The drone's heading now, degrees counter-clockwise from +x, or None."""
    yaw = snapshot.get("stabilizer.yaw")
    return None if yaw is None else float(yaw)


#: How long the connection handshake may take before it is called a failure.
#:
#: cflib's SyncCrazyflie.open_link() waits on an Event with NO TIMEOUT
#: (syncCrazyflie.py: `self._connect_event.wait()`), so a radio unplugged
#: mid-handshake, or a drone that never finishes its TOC exchange, blocks the
#: calling thread for good. That wedged the desktop app on 2026-09-22: the
#: checks worker never returned, and every later action answered "Something is
#: already running" — true, and useless.
#:
#: A healthy connect measured 0.7 s to scan and about 2 s to finish the TOCs
#: from cache, so 20 s is far beyond anything normal and still short enough
#: that an operator is told rather than left watching a spinner.
CONNECT_TIMEOUT_S = 20.0
#: How long closing a stalled handshake may take before it is left behind.
ABANDON_CLOSE_S = 3.0


class DroneLink:
    def __init__(
        self,
        uri: str = core.DEFAULT_URI,
        *,
        scf_factory: Callable[[str], Any] = _default_scf,
        scan: Callable[[], list[str]] = _default_scan,
        stream_factory: Callable[[Any], TelemetryStream] = _default_stream,
        configure: Callable[[Any], None] = core._configure,
        cut_motors: Callable[[Any], None] = core._cut_motors,
    ) -> None:
        self.uri = uri
        self._scf_factory = scf_factory
        self._scan = scan
        self._stream_factory = stream_factory
        self._configure = configure
        self._cut_motors = cut_motors
        self.scf: Any = None
        #: The missing-Lighthouse-memory warning, said once per link.
        self._said_no_lighthouse_memory = False
        self.stream: TelemetryStream | None = None
        self._tuning: FlightTuning | None = None
        #: What the last manual flight changed, for the audit trail and history.
        self.tuning_applied: list[Applied] = []

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self.scf is not None

    def open(self) -> None:
        if self.is_open:
            return
        try:
            found = self._scan()
        except Exception as e:
            # On Windows this is also what a dongle with no driver looks like.
            raise LinkError(unopenable_reason()) from e
        if not found:
            raise LinkError(_nothing_found_reason())

        scf = self._scf_factory(self.uri)
        self._open_link_within(scf, CONNECT_TIMEOUT_S)

        try:
            self._configure(scf.cf)
            stream = self._stream_factory(scf)
            stream.start()
        except Exception:
            self._safe_close(scf)
            raise
        self.scf, self.stream = scf, stream
        log.info("link open to %s", self.uri)

    @staticmethod
    def _open_link_within(scf: Any, timeout_s: float) -> None:
        """Open the link, or give up and say so — never wait for ever.

        The handshake runs on its own thread because cflib's wait cannot be
        interrupted. On a timeout that thread is abandoned: it is a daemon, so
        it dies with the process, and leaking one blocked thread is much
        cheaper than an app that can never do anything again.
        """
        failure: list[BaseException] = []

        def connect() -> None:
            try:
                scf.open_link()
            except BaseException as e:                    # noqa: BLE001
                failure.append(e)

        worker = threading.Thread(target=connect, daemon=True, name="cf-connect")
        worker.start()
        worker.join(timeout_s)

        if worker.is_alive():
            DroneLink._abandon(scf)
            raise LinkError(
                f"The drone answered the radio but did not finish connecting (no reply "
                f"for {timeout_s:.0f} s). Switch the drone off and on again — it reconnects "
                f"by itself. If it keeps happening, charge the battery and check the "
                f"Crazyradio is plugged in."
            )
        if failure:
            raise LinkError(
                f"The drone was found but the link failed to open ({failure[0]})."
            ) from failure[0]

    @staticmethod
    def _abandon(scf: Any) -> None:
        """Close a link whose handshake never finished — the radio half of it.

        cflib's SyncCrazyflie.close_link does NOTHING until the handshake has
        completed (it checks is_link_open), so the Crazyflie's own close_link
        is called. Left open, the stalled link's radio thread kept polling the
        Crazyradio, and every later attempt shared the dongle with it.
        Measured 2026-10-24: after one stall, every standby attempt for the
        next ten minutes reached "Connected" at the radio and never finished
        setting up, the agent at 32 % CPU — while the drone sat on and the
        dongle stayed plugged in. Bounded: a close that blocks is abandoned
        too, on a daemon thread, rather than wedging the caller.
        """
        cf = getattr(scf, "cf", None)
        close = getattr(cf, "close_link", None)
        if close is None:
            return

        def shut() -> None:
            try:
                close()
            except Exception:                              # noqa: BLE001
                log.debug("closing an abandoned link failed — it is dropped anyway")

        closer = threading.Thread(target=shut, daemon=True, name="cf-abandon")
        closer.start()
        closer.join(ABANDON_CLOSE_S)

    def close(self) -> None:
        if not self.is_open:
            return
        scf, stream = self.scf, self.stream
        self.scf, self.stream = None, None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                log.debug("stream stop failed during close")
        self._safe_close(scf)
        log.info("link closed")

    def _safe_close(self, scf: Any) -> None:
        # Motors stopped on every path out, whatever state the link is in.
        try:
            self._cut_motors(scf)
        finally:
            try:
                scf.close_link()
            except Exception:
                log.debug("close_link failed — link probably already down")

    def __enter__(self) -> DroneLink:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ── standby: what a link with no session needs ───────────────────────

    def identify(self) -> str:
        """The drone's hardware id — a param read, what a link with no session
        shows. Never arms."""
        scf, _ = self._require_open()
        return read_hardware_id(scf.cf)

    def station_poses(self, timeout_s: float = 5.0) -> list[Any]:
        """Where the base stations are and which way they face, as stored on
        the drone by `dronedeck geometry` — the input to the predicted
        coverage (mission/plan/coverage.py). Empty when no geometry is stored,
        no link is open, or the drone does not answer in time. Read-only."""
        from cflib.crazyflie.mem import LighthouseMemHelper

        from dronedeck.mission.plan.coverage import StationPose

        if self.scf is None:
            return []
        done, geos = threading.Event(), {}

        def read(found: dict) -> None:
            geos.update(found)
            done.set()

        try:
            LighthouseMemHelper(self.scf.cf).read_all_geos(read)
        except Exception as e:                                   # noqa: BLE001
            # cflib raises "Unexpected nr of memories found: 0" when the drone
            # detected no Lighthouse deck at boot — decks unpowered or loose,
            # not a fault here. Said once per link, not with a traceback every
            # few seconds (2026-10-24: it filled the log).
            if "nr of memories" in str(e):
                if not self._said_no_lighthouse_memory:
                    log.warning("no Lighthouse deck memory on this drone (%s) — the "
                                "decks were not detected at power-on", e)
                    self._said_no_lighthouse_memory = True
            else:
                log.exception("could not read the base station geometry")
            return []
        if not done.wait(timeout_s):
            return []
        def vec(v: Any) -> tuple[float, float, float]:
            return (float(v[0]), float(v[1]), float(v[2]))

        return [StationPose(vec(g.origin), tuple(vec(row) for row in g.rotation_matrix),
                            station=int(bs))
                for bs, g in sorted(geos.items()) if getattr(g, "valid", False)]

    def on_lost(self, callback: Callable[[str], None]) -> None:
        """Call `callback(reason)` once if the radio link drops by itself.

        cflib reports it on its own thread, and closing the link from inside
        that callback can deadlock cflib — so the callback must not close the
        link itself; it should hand the work to another thread.
        """
        scf, _ = self._require_open()
        fired = threading.Event()

        def lost(_uri: str, message: str) -> None:
            if not fired.is_set():
                fired.set()
                callback(str(message))

        scf.cf.connection_lost.add_callback(lost)

    # ── reading ──────────────────────────────────────────────────────────

    def _require_open(self) -> tuple[Any, TelemetryStream]:
        if self.scf is None or self.stream is None:
            raise LinkError("Not connected to a drone.")
        return self.scf, self.stream

    def snapshot(self) -> Snapshot:
        _, stream = self._require_open()
        return stream.snapshot()

    # ── checks ───────────────────────────────────────────────────────────

    def checks(self) -> Generator[CheckResult, None, ReadyReport]:
        scf, stream = self._require_open()
        return run_checks(
            scf.cf, stream.snapshot,
            reset_estimator=lambda: preflight.reset_estimator(scf.cf),
            request_recovery=lambda: self._request_crash_recovery(scf.cf),
        )

    @staticmethod
    def _request_crash_recovery(cf: Any) -> None:
        """cflib 0.1.33 moved this to ``cf.supervisor``; older builds had it on
        ``cf.platform``. Whichever this cflib has."""
        target = getattr(cf, "supervisor", None) or getattr(cf, "platform", None)
        if target is None or not hasattr(target, "send_crash_recovery_request"):
            log.warning("this cflib cannot request crash recovery")
            return
        target.send_crash_recovery_request()
        log.info("crash recovery requested")

    def prop_test(self) -> PropTestResult:
        """Spins the motors briefly. The caller must hold the operator's
        confirmation that the drone is on the floor and clear."""
        scf, _ = self._require_open()

        def read_health() -> tuple[int, int]:
            row = preflight.sample(
                scf, [("health.motorTestCount", "uint16_t"), ("health.motorPass", "uint8_t")], n=1
            )[0]
            return int(row["health.motorTestCount"]), int(row["health.motorPass"])

        return run_prop_test(scf.cf, read_health)

    def battery_test(self) -> BatteryTestResult:
        """Spins all four motors briefly under load. Same precondition."""
        scf, stream = self._require_open()

        def read_result() -> tuple[float, int]:
            row = preflight.sample(
                scf, [("health.batterySag", "float"), ("health.batteryPass", "uint8_t")], n=1
            )[0]
            return float(row["health.batterySag"]), int(row["health.batteryPass"])

        return run_battery_test(scf.cf, read_result, stream.snapshot)

    def health_test(self) -> HealthTestResult:
        """Propellers one at a time, then the battery under all four.

        A motor that fails its own test still lets the battery half run: the two
        answer different questions, and the operator needs both to decide.
        """
        motors = self.prop_test()
        try:
            battery = self.battery_test()
        except Exception as e:
            log.exception("battery test did not report")
            return HealthTestResult(
                motors, None, f"The battery test did not report ({type(e).__name__}).")
        return HealthTestResult(motors, battery)

    # ── flying ───────────────────────────────────────────────────────────

    def guarded_flight(
        self,
        report: ReadyReport,
        *,
        target_height_m: float | None,
        hold_position: bool = True,
        fence_half_extent_m: float = DEFAULT_FENCE_M,
        max_height_m: float = DEFAULT_MAX_HEIGHT_M,
        on_phase: Callable[[PhaseEvent], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> GuardedFlight:
        scf, stream = self._require_open()
        guard = FlightGuard(GuardContext(
            ground_z=report.ground_z_m,
            fence_half_extent_m=fence_half_extent_m,
            max_height_m=max_height_m,
            takeoff_xy=report.takeoff_xy if hold_position else None,
            target_height_m=target_height_m,
            assisted=report.assisted,
        ))
        return GuardedFlight(
            scf.cf, stream.snapshot, guard, report,
            clock=clock, sleep=sleep, on_phase=on_phase,
        )

    def manual(
        self,
        report: ReadyReport,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> ManualController:
        """The manual controller for this flight.

        Unassisted, the drone is switched to the complementary estimator, whose
        height comes from the barometer, and the floor is read from it once it
        has had a moment to settle. The Kalman ground height in the report is
        meaningless without base stations and is not used.
        """
        scf, stream = self._require_open()
        ground_z = report.ground_z_m
        # Tuning first, so the estimator settles under the gains it will fly on.
        # See flight/tuning.py for the lab evidence behind every value.
        self._tuning = FlightTuning(getattr(scf.cf, "param", None))
        profiles = (BASE_PROFILE,) if report.assisted else (BASE_PROFILE, BAROMETER_PROFILE)
        self.tuning_applied = self._tuning.apply(*profiles)
        if not report.assisted:
            scf.cf.param.set_value(ESTIMATOR_PARAM, ESTIMATOR_COMPLEMENTARY)
            sleep(ESTIMATOR_SETTLE_S)
            ground_z = self._average_z(sleep)
            log.info("unassisted flight: barometer ground at z=%.3f", ground_z)
        return ManualController(
            scf.cf.commander,
            ground_z=ground_z,
            land=lambda z, duration: scf.cf.high_level_commander.land(z, duration),
            assisted=report.assisted,
            # Only assisted flight has a position worth holding. Unassisted
            # there are no base stations, so x and y are dead reckoning and
            # anchoring to them would fly the drone into the drift.
            position=(lambda: _fix(stream.snapshot())) if report.assisted else None,
            # The heading, with or without base stations: it is what keeps the
            # arrows in the room's directions when there is no position to
            # carry them (keyframe.py, "takeoff").
            heading=lambda: _heading(stream.snapshot()),
        )

    def restore_estimator(self) -> None:
        """Put the drone back as the flight found it: the flight tuning's exact
        original values, and the Kalman estimator so the next checks see the
        base stations. Called after every manual flight, assisted or not."""
        if self._tuning is not None:
            self._tuning.restore()
            self._tuning = None
        if self.scf is None:
            return
        try:
            self.scf.cf.param.set_value(ESTIMATOR_PARAM, ESTIMATOR_KALMAN)
        except Exception:
            log.warning("could not restore the Kalman estimator")

    def _average_z(self, sleep: Callable[[float], None], samples: int = 10) -> float:
        values: list[float] = []
        for _ in range(samples * 3):
            z = self.snapshot().get("stateEstimate.z")
            if z is not None:
                values.append(z)
                if len(values) >= samples:
                    break
            sleep(0.1)
        return sum(values) / len(values) if values else 0.0

    def manual_guard(
        self,
        report: ReadyReport,
        *,
        fence_half_extent_m: float = DEFAULT_FENCE_M,
        max_height_m: float = DEFAULT_MAX_HEIGHT_M,
        fence: Callable[[float, float], bool] | None = None,
    ) -> FlightGuard:
        """Manual flight moves on purpose, so no drift or height-error checks.

        `fence` is a mission's room geofence (whether a point is inside it);
        without one the guard keeps the square of `fence_half_extent_m`.
        """
        return FlightGuard(GuardContext(
            ground_z=report.ground_z_m,
            fence_half_extent_m=fence_half_extent_m,
            max_height_m=max_height_m,
            assisted=report.assisted,
            fence=fence,
        ))
