# This file was modified on 11/4/2026 with the assistance of Claude Code in VS Code
# (Claude Opus 5.5, Anthropic, 2026). The station status reports the position
# uncertainty rounded up to 0.1 cm, so a reading the 5 cm check refuses never displays
# as within the limit.

"""An operating session: one operator, one drone, from sign-in to sign-off.

This is what the desktop app drives, and the only thing that flies a drone.

    sign_in            who is responsible for what follows
    start              connect, then the checks, one step at a time
    confirm_area       the operator says the drone is on the floor and clear
    health_test        the firmware's motor and battery tests (motors spin, briefly)
    retry              after an abnormal end: every check again, same session
    run_program        Auto: a preset flight, e.g. the hover test
    run_mission        Auto: a saved mission, flown by the mission controller
                       through the manual flight system
    arm_manual         Manual: props idle, then assisted flight from the keys
    land / stop        graceful landing, or the deliberate emergency stop
    end                land if flying, close the flight, upload what is left

Everything long-running happens on a worker thread and reports progress as
events, so the app can show a live checklist instead of a frozen button.

Rules this file exists to hold in one place:

- **Nothing flies until the checks pass and the operator confirms the area.**
- **Nothing flies without a signed-in operator**, so every flight and every
  action has a person attached.
- **Only one activity at a time.** The radio is single-user (a concurrent scan
  produced `[Errno 19] No such device` in the lab).
- **A flight always ends in a landing or a stop**, including when an exception
  escapes: the drone must never be left in the air.
- **After an abnormal end, nothing flies until Retry.** A tumble leaves the
  firmware holding the motors; arming past that is a flight with no thrust.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from dronedeck.audit import Action, AuditLog, Result
from dronedeck.flight import supervisor as motor_supervisor
from dronedeck.flight.checks import CheckResult, ChecksFailed, ReadyReport, collect
from dronedeck.flight.control import GuardedFlight, PhaseEvent
from dronedeck.flight.controls_store import ControlsStore, valid_spot
from dronedeck.flight.core import DEFAULT_URI
from dronedeck.flight.geometry import SweepAngles, channel_name, estimate_quick
from dronedeck.flight.keyframe import FrameStatus, KeyFrame
from dronedeck.flight.link import DEFAULT_FENCE_M, DEFAULT_MAX_HEIGHT_M, DroneLink, LinkError
from dronedeck.flight.manual import CLIMB_RATE_M_S, MOVE_SPEED_M_S
from dronedeck.flight.position_stages import first_blocker
from dronedeck.flight.preflight import reset_estimator
from dronedeck.flight.programs import HoverTest, Outcome, run_hover_test
from dronedeck.history import (
    SessionLog,
    SessionMeta,
    interrupted_flights,
    interrupted_sessions,
    sessions_dir,
    set_flight_processing,
    set_session_processing,
)
from dronedeck.mission.controller import (
    TERMINAL_STATES,
    EventKind,
    MissionController,
    MissionEvent,
    MissionState,
)
from dronedeck.mission.plan.coverage import Prediction, Survey, predict
from dronedeck.mission.plan.fit import FlyingPlan, Move, plan_to_fly
from dronedeck.mission.plan.floorplan import PlanError, Room
from dronedeck.mission.plan.geofence import Geofence
from dronedeck.mission.plan.mission import Mission
from dronedeck.mission.plan.store import NotFound, PlanStore
from dronedeck.mission.plan.validate import errors, flyable_bound
from dronedeck.paths import flights_dir
from dronedeck.processing import LIVE, SESSION, Job, JobState, ProcessingQueue
from dronedeck.safety.flight_guard import Action as GuardAction
from dronedeck.safety.flight_guard import Reason as GuardReason
from dronedeck.safety.flight_guard import Verdict as GuardVerdict
from dronedeck.safety.flight_guard import (
    assess_positioning,
    position_trusted,
    station_ids,
    uncertainty_cm,
)
from dronedeck.sync import auth_store, flight_playback
from dronedeck.sync.cloud import AuthError, Cloud, CloudTimeout, Operator
from dronedeck.sync.outbox import Kind, Outbox, new_id
from dronedeck.sync.syncer import Syncer
from dronedeck.telemetry import trace as flight_trace
from dronedeck.telemetry.reader import FlightRecorder
from dronedeck.telemetry.row import TempUnit, parse_ambient
from dronedeck.telemetry.sinks import CsvSink, FanOutSink, LiveUploadSink

log = logging.getLogger(__name__)

#: What a session is for (Snapshot.purpose). A CAPTURE session is the Capture
#: page's (2026-10-24, Samuel: "the capture page only cares about taking picture
#: manually and not the rest"): it checks the radio and the camera's deck only
#: (checks.py run_capture_checks), records only what is captured by hand, and
#: refuses every flight action — in the agent, not only on the page.
FLIGHT = "flight"
CAPTURE_PURPOSE = "capture"
PURPOSES = (FLIGHT, CAPTURE_PURPOSE)
CAPTURE_OPEN = ("Capture session open. Take pictures on the Capture page — this session "
                "does not fly.")
CAPTURE_NO_FLIGHT = ("This is a capture session: it takes pictures and does not fly. End it "
                     "on the Capture page, then start a session on Control to fly.")

MANUAL_GUARD_PERIOD_S = 0.1
#: The lowest height Manual's map counts the stations' reach at, metres above
#: the origin (where the drone sat on the floor to measure them): a drone
#: resting on its legs is ~3 cm up, and the props' first lift is not flying.
MANUAL_SPACE_FLOOR_M = 0.1

#: How often a signed-in agent with no drone looks for one. NOT faster: polling
#: the radio every 2 s (from /status) wedged the agent with [Errno 19] No such
#: device — sessions-and-modes.txt. It never scans while a link is open.
STANDBY_RETRY_S = 15.0
#: After a drone drops off or a session ends, look again this soon.
STANDBY_SOON_S = 2.0
#: After a restart over the radio, the drone takes ~3 s to answer again
#: (measured 2026-09-24: power-cycle at 1.0 s, link back at 4.6 s). Looking
#: sooner starts a connect that blocks for its full timeout.
RESTART_SETTLE_S = 4.0
#: Reset drone: how long past the settle it keeps trying to reconnect before
#: saying the drone did not come back, and how often it tries.
RESET_RECONNECT_S = 12.0
RESET_RECONNECT_POLL_S = 1.0
#: Reset drone mid-program: how long to let the program's worker wind down
#: after the stop, before the reset takes over.
RESET_WORKER_WAIT_S = 5.0


def _power_cycle(uri: str) -> None:
    """Restart the drone's electronics through its radio chip (cflib)."""
    from cflib.utils.power_switch import PowerSwitch

    switch = PowerSwitch(uri)
    try:
        switch.stm_power_cycle()
    finally:
        switch.close()


class Mode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class State(StrEnum):
    SIGNED_OUT = "signed_out"
    IDLE = "idle"                               # signed in, no drone session
    STARTING = "starting"                       # connecting and checking
    CHECKS_FAILED = "checks_failed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    READY = "ready"                             # confirmed; may fly
    BUSY = "busy"                               # prop test, program or manual
    ENDING = "ending"


#: How long a flight may be "armed or flying" with the motors at zero before it
#: is called what it is. Arming ramps the props within ~0.3 s (trace, 2026-10-05).
MOTORS_START_GRACE_S = 1.5


def _motors_not_spinning(snap: Any, armed_for_s: float) -> str | None:
    """Why a flight that says it is flying is not, or None.

    2026-10-05: three missions ran "armed → flying → landing" in the flight
    system while the drone's supervisor was LOCKED and thrust stayed 0 — the
    app said started and landed; the drone never moved. LOCKED is said at
    once; zero thrust on every motor past MOTORS_START_GRACE_S otherwise."""
    info = snap.get("supervisor.info")
    if info is not None and int(info) & motor_supervisor.IS_LOCKED:
        return ("The motors did not start: the drone's supervisor is LOCKED (it locks "
                "after some landings). Nothing flew. Press Reset drone, then start again.")
    motors = [snap.get(f"motor.m{i}") for i in range(1, 5)]
    if armed_for_s >= MOTORS_START_GRACE_S and all(m is not None and m <= 0 for m in motors):
        return ("The motors did not start: all four read zero after arming. Nothing flew. "
                "Press Reset drone, then start again.")
    return None


@dataclass(frozen=True)
class Blocker:
    """One reason a mission cannot start now, and what to do about it."""

    code: str
    message: str
    fix: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "fix": self.fix}


class SessionError(RuntimeError):
    """A refusal the operator should see. Never leaks internals."""


@dataclass
class Snapshot:
    """What the app renders. Serialised straight to JSON."""

    state: State = State.SIGNED_OUT
    # MANUAL, not AUTO. Auto needs `assisted` — a position the drone can hold —
    # and a preset program is refused without it (see run_program below, and
    # flight-safety.txt). Until this room's Lighthouse geometry is measured,
    # every session comes back unassisted, so an Auto default opens the app in
    # the one mode that cannot fly. It also filtered the history: an operator
    # whose flights were all manual saw "no sessions" on every log page with 45
    # readings on disk (2026-09-17).
    #
    # Tests that exercise Auto set the mode explicitly rather than inheriting
    # it, which is what a test about Auto should do anyway.
    mode: Mode = Mode.MANUAL
    operator: dict[str, Any] | None = None
    drone: dict[str, Any] | None = None
    session_id: str | None = None
    activity: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    #: The last battery & motor test (flight/checks.py HealthTestResult).
    health_test: dict[str, Any] | None = None
    flight: dict[str, Any] | None = None
    message: str | None = None
    can_fly: bool = False
    #: True until the saved sign-in has been tried, at startup. The app shows a
    #: loading state while it is set: signed out is not yet known to be true.
    restoring: bool = True
    #: True when the drone can hold a height and a position for itself. False
    #: means the app must offer unassisted manual flight only, and say so.
    assisted: bool = True
    #: Why assistance is unavailable, in words, or None.
    unassisted_reason: str | None = None
    #: Whether the AI deck — the one carrying the camera — is fitted, as the
    #: DRONE reports it (deck.bcAI, over the radio during the checks). None
    #: until a drone has been asked, which is NOT the same as "not fitted".
    #: Recorded and shown; it gates nothing.
    ai_deck: bool | None = None
    #: The drone reported its AI deck earlier in this agent's life and now
    #: does not: the decks lost power or contact, not a drone without one.
    #: Only a power-off brings them back (deck detection runs at boot) — the
    #: app says so instead of "not fitted" (2026-10-24: a restart over the
    #: radio left the decks unpowered and the page read "no camera on this
    #: drone").
    decks_lost: bool = False
    #: The radio, apart from any session. "off" (signed out, or not started),
    #: "searching" (no drone found yet — `message` says why), "connected" (a
    #: link is open: vitals stream, the camera gets its network), "paused" (the
    #: operator disconnected, to free the radio for another tool), "restarting"
    #: (restarted over the radio on purpose — restart_drone — back in seconds). A session
    #: takes over a connected link rather than opening a second one.
    radio: dict[str, Any] = field(default_factory=lambda: {
        "state": "off", "hardware_id": None, "message": None})
    #: The last flight ended abnormally — a tumble, a guard, an emergency stop.
    #: Nothing flies until Retry has re-run the checks in this same session:
    #: after a tumble the firmware holds the motors at zero, and a flight armed
    #: into that reports "flying" with nothing turning (2026-09-17).
    retry_required: bool = False
    #: The mission being flown, or the last one flown this session: its id,
    #: name, revision, the mission controller's state, the point being held,
    #: the points completed, and the last event. None when no mission has run.
    mission: dict[str, Any] | None = None
    #: The DPP switch and the flights being processed (processing.py). `on` is
    #: what the next flight will get; `chosen` is False while it is still the
    #: default (on, in either mode); `jobs` newest first;
    #: `last_flight_id` the session's most recent flight to land, so a flight
    #: flown with DPP off can still be processed from the page.
    processing: dict[str, Any] = field(default_factory=lambda: {
        "on": True, "chosen": False, "jobs": [], "last_flight_id": None})
    #: The auto-capture switch (camera/recording.py): `auto` is whether the
    #: camera's frames are recorded automatically; `chosen` False while it is
    #: still the default (on). Manual captures are saved either way.
    capture: dict[str, Any] = field(default_factory=lambda: {"auto": True, "chosen": False})
    #: What the session is for: "flight" (Control — every check, flying) or
    #: "capture" (the Capture page — the radio and the camera's deck only, and
    #: nothing flies). Kept while a session is open; "flight" otherwise.
    purpose: str = "flight"
    #: Which way the arrow keys move the drone (flight/keyframe.py): the
    #: operator's choice (`key_frame`, "operator" or "room"), their marked
    #: spot (`operator`, room metres, or None for the takeoff spot) and, while
    #: flying, what the arrows mean right now (`live`: the active frame, why
    #: it is not the chosen one, the spot in use). `live` is None on the ground.
    controls: dict[str, Any] = field(default_factory=lambda: {
        "key_frame": "operator", "operator": None, "operator_marked_at": None,
        "live": None})

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state), "mode": str(self.mode), "operator": self.operator,
            "drone": self.drone, "session_id": self.session_id, "activity": self.activity,
            "checks": self.checks, "health_test": self.health_test, "flight": self.flight,
            "message": self.message, "can_fly": self.can_fly, "restoring": self.restoring,
            "assisted": self.assisted, "unassisted_reason": self.unassisted_reason,
            "ai_deck": self.ai_deck, "decks_lost": self.decks_lost,
            "radio": self.radio,
            "retry_required": self.retry_required,
            "mission": self.mission,
            "processing": self.processing,
            "capture": self.capture,
            "purpose": self.purpose,
            "controls": self.controls,
        }


class Session:
    def __init__(
        self,
        *,
        cloud: Cloud,
        outbox: Outbox | None = None,
        syncer: Syncer | None = None,
        link_factory: Callable[[], DroneLink] = DroneLink,
        publish: Callable[[str, dict[str, Any]], None] | None = None,
        agent_id: str = "desktop",
        fence_half_extent_m: float = DEFAULT_FENCE_M,
        max_height_m: float = DEFAULT_MAX_HEIGHT_M,
        on_link_ready: Callable[[Any], object] | None = None,
        on_link_down: Callable[[], object] | None = None,
        on_link_no_deck: Callable[[], object] | None = None,
        on_session_open: Callable[[str, Path], object] | None = None,
        on_session_close: Callable[[], object] | None = None,
        power_cycle: Callable[[str], None] = _power_cycle,
        plans: PlanStore | None = None,
        mission_controller: Callable[..., Any] = MissionController,
        processing: ProcessingQueue | None = None,
        controls: ControlsStore | None = None,
    ) -> None:
        self._cloud = cloud
        #: The arrow keys' frame and the operator's spot, kept on the laptop.
        self._controls_store = controls or ControlsStore()
        self._controls = self._controls_store.load()
        self._controls_live: dict[str, Any] | None = None
        #: Flights waiting for the data pipeline, run in a process of their own.
        self.processing = processing or ProcessingQueue()
        #: The operator's DPP choice for this session, or None for the mode's
        #: default. Reset when a session ends: the choice is per session.
        self._processing_choice: bool | None = None
        #: The operator's auto-capture choice for this session, or None for the
        #: default (on). Reset when a session ends, like the DPP's.
        self._capture_choice: bool | None = None
        #: Whether the flight recording now is to be processed when it ends —
        #: read as it began, so a switch flipped mid-flight is for the next one.
        self._flight_process = False
        #: The session's most recent flight to land (Snapshot.processing).
        self._last_flight_id: str | None = None
        #: Story 4.9: each inspection point's verdict while the mission flies —
        #: {point id: {verdict, reasons, findings, ...}} for the flight in the air.
        self._live_flight_id: str | None = None
        self._live_points: dict[str, dict[str, Any]] = {}
        #: Rooms and missions on this laptop (mission/plan/store.py).
        self.plans = plans or PlanStore()
        #: Builds the mission controller for a flight. A parameter so tests can
        #: fly the session's mission wiring with a controller of their own.
        self._mission_controller = mission_controller
        #: Called with the Crazyflie once a link has passed its checks and the
        #: AI deck is fitted — how the deck is put on the operator's Wi-Fi. It
        #: runs on its own thread: joining a network takes seconds and must
        #: never hold up a flight.
        self._on_link_ready = on_link_ready
        #: Called whenever the link closes, however — the Wi-Fi state stops
        #: claiming "joined" for a drone nobody can reach any more.
        self._on_link_down = on_link_down
        #: Called when a link opens on a drone that reports NO AI deck: the
        #: camera cannot be handed the network, and the Wi-Fi page must say
        #: why instead of "waiting for the drone to connect" (2026-10-24).
        self._on_link_no_deck = on_link_no_deck
        #: A session opened (its id and folder) / closed — how camera frames
        #: get recorded only inside a session (camera/recording.py).
        self._on_session_open = on_session_open
        self._on_session_close = on_session_close
        self._power_cycle = power_cycle
        self._outbox = outbox or Outbox()
        self._syncer = syncer
        self._link_factory = link_factory
        self._publish = publish
        self._agent_id = agent_id
        self._fence = fence_half_extent_m
        self._max_height = max_height_m

        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        #: What that worker is, and when it started, so a session that will not
        #: free up can say which step is stuck instead of "something".
        self._worker_name: str | None = None
        self._worker_started_at: float | None = None
        self._snapshot = Snapshot()
        self._snapshot.controls = self._controls_dict()
        self.operator: Operator | None = None
        self.audit: AuditLog | None = None

        self.link: DroneLink | None = None
        self.report: ReadyReport | None = None
        self.flight: GuardedFlight | None = None
        self.manual: Any = None
        #: The mission controller while a mission flies; None otherwise.
        self.mission: Any = None
        self._mission_plan: Mission | None = None
        #: The coverage survey in progress: (room id, the survey), or None.
        self._survey: tuple[str, Survey] | None = None
        #: A base station measurement is reading the drone right now.
        self._measuring = False
        #: The stations' stored poses for the link that read them (_station_poses).
        self._poses_cache: tuple[Any, list[Any]] | None = None
        #: Drones (hardware ids) that reported an AI deck since the agent
        #: started — what makes a later "no deck" a LOST one (decks_lost).
        self._decks_seen: set[str] = set()
        #: Whether the positioning deck needs its firmware, per link (it can
        #: only change at power-on, which is a new link).
        self._deck_fw_cache: tuple[Any, bool] | None = None
        #: Manual's space, kept with the poses it was computed from (by
        #: identity: _station_poses hands back the same list until they change).
        self._space_cache: tuple[list[Any], Prediction] | None = None
        self._manual_guard_stop: threading.Event | None = None
        self._recorder: FlightRecorder | None = None
        self._flight_id: str | None = None
        self._unsubscribe_telemetry: Callable[[], None] | None = None
        self.history: SessionLog | None = None
        # The floor, in the estimator frame the current flight is using. The
        # checks' ground unless an unassisted flight re-read it from the baro.
        self._height_reference: float | None = None
        self._manual_lock = threading.Lock()
        #: Held for the whole of closing a manual flight, so the guard thread
        #: and Reset drone cannot interleave: whichever closes it, the other
        #: waits and then finds nothing to do — and never lands its "Landed" or
        #: "Motors stopped" state on top of a reset already under way.
        self._finish_lock = threading.RLock()
        #: Reset drone's waits, on the instance so a test can shorten them.
        self._reset_settle_s = RESTART_SETTLE_S
        self._reset_reconnect_s = RESET_RECONNECT_S
        self._trace: flight_trace.FlightTrace | None = None
        self._unsubscribe_trace: Callable[[], None] | None = None
        #: Opening and closing `self.link` happen under this, whoever does it —
        #: the standby loop, a session start, a retry, an end. Never held while
        #: waiting on anything but the radio itself.
        self._link_lock = threading.RLock()
        #: The link the Wi-Fi hand-off last ran for, so it runs once per link.
        self._link_ready_for: int | None = None
        self._standby_wake = threading.Event()
        self._standby_stop = threading.Event()
        self._standby_paused = False
        self._standby_thread: threading.Thread | None = None
        #: Standby does not look before this (monotonic): a drone restarting.
        self._standby_not_before = 0.0

    # ── events ───────────────────────────────────────────────────────────

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self._publish is not None:
            try:
                self._publish(kind, payload)
            except Exception:
                log.exception("event publish failed")

    def _set(self, **changes: Any) -> None:
        with self._lock:
            if "ai_deck" in changes:
                changes["decks_lost"] = self._decks_lost_locked(changes)
            for key, value in changes.items():
                setattr(self._snapshot, key, value)
            self._snapshot.can_fly = self._snapshot.state in (State.READY, State.BUSY)
            payload = self._snapshot.to_dict()
        self._emit("session", payload)

    def _decks_lost_locked(self, changes: dict[str, Any]) -> bool:
        """Every report of the AI deck comes through _set: remember the drones
        that had one, and call a later "no deck" on the same drone LOST."""
        fitted = changes["ai_deck"]
        hardware_id = ((changes.get("radio") or {}).get("hardware_id")
                       or (changes.get("drone") or {}).get("hardware_id")
                       or (self._snapshot.radio or {}).get("hardware_id"))
        if fitted is None or hardware_id is None:
            return self._snapshot.decks_lost
        if fitted:
            self._decks_seen.add(hardware_id)
            return False
        return hardware_id in self._decks_seen

    # ── auth ─────────────────────────────────────────────────────────────

    def sign_in(self, email: str, password: str) -> Operator:
        with self._lock:
            if self._snapshot.state not in (State.SIGNED_OUT, State.IDLE):
                raise SessionError("End the current session before signing in again.")
        try:
            operator = self._cloud.sign_in(email, password)
        except AuthError as e:
            raise SessionError(str(e)) from None
        if not operator.is_operator:
            self._cloud.sign_out()
            raise SessionError(
                "This account can view flights but not fly. Ask an operator to change your role."
            )

        self.operator = operator
        self.audit = AuditLog(
            self._outbox, actor_id=operator.id, actor_email=operator.email, source="desktop"
        )
        self.audit.record(Action.SIGN_IN)
        self._remember_sign_in(operator)
        self._signed_in(operator)
        return operator

    def restore_sign_in(self) -> Operator | None:
        """Sign back in from the token saved last time, if there is one.

        Called once at startup. A revoked or expired token is deleted and the
        app shows the sign-in form, exactly as if nothing had been saved.
        """
        with self._lock:
            if self._snapshot.state is not State.SIGNED_OUT:
                self._snapshot.restoring = False
                return self.operator
        stored = auth_store.load()
        restore = getattr(self._cloud, "restore", None)
        if stored is None or restore is None:
            self._set(restoring=False)
            return None
        token, _email = stored
        try:
            operator: Operator = restore(token)
        except CloudTimeout as e:
            # Supabase did not answer: the saved sign-in may be perfectly good,
            # so it is kept for the next launch. Deleting it here would sign
            # the operator out because of a slow network.
            self._set(message=str(e), restoring=False)
            return None
        except AuthError as e:
            auth_store.clear()
            self._set(message=str(e), restoring=False)
            return None
        if not operator.is_operator:
            auth_store.clear()
            self._cloud.sign_out()
            self._set(restoring=False)
            return None
        self.operator = operator
        self.audit = AuditLog(
            self._outbox, actor_id=operator.id, actor_email=operator.email, source="desktop"
        )
        self.audit.record(Action.SIGN_IN, detail={"restored": True})
        self._remember_sign_in(operator)       # refresh tokens rotate on use
        self._signed_in(operator)
        return operator

    def _remember_sign_in(self, operator: Operator) -> None:
        token = getattr(self._cloud, "refresh_token", None)
        if isinstance(token, str) and token:
            try:
                auth_store.save(token, operator.email)
            except OSError:
                log.warning("could not save the sign-in; the next launch will ask again")

    def _signed_in(self, operator: Operator) -> None:
        self._set(
            restoring=False,
            state=State.IDLE,
            operator={"id": operator.id, "email": operator.email, "name": operator.full_name,
                      "role": operator.role},
            message=None,
        )
        if self._syncer is not None:
            self._syncer.trigger()
        self._wake_standby(0)

    def sign_out(self) -> None:
        if self.snapshot().state not in (State.SIGNED_OUT, State.IDLE):
            self.end("signed out")
        # A drone held on standby is released: no one is signed in to see it.
        if self.snapshot().state == State.IDLE:
            self._close_link()
        if self.audit is not None:
            self.audit.record(Action.SIGN_OUT)
        if self._syncer is not None:
            self._syncer.trigger()
        self._cloud.sign_out()
        auth_store.clear()
        self.operator, self.audit = None, None
        self._set(state=State.SIGNED_OUT, operator=None, message=None, restoring=False,
                  radio={"state": "off", "hardware_id": None, "message": None})

    def _require_operator(self) -> tuple[Operator, AuditLog]:
        if self.operator is None or self.audit is None:
            raise SessionError("Sign in before operating the drone.")
        return self.operator, self.audit

    # ── mode ─────────────────────────────────────────────────────────────

    def set_mode(self, mode: Mode) -> None:
        """Choose the mode the NEXT session opens in.

        A SESSION BELONGS TO THE MODE IT STARTED IN (2026-10-01, the owner's
        "nothing should bleed between Manual and Auto"). It was checked and
        confirmed under that mode's rules — Manual accepts an unassisted drone,
        Auto never does — so the mode cannot change while one is open, flying
        or not. End it, and the next session can open in the other mode. While
        a session is open, Snapshot.mode IS that session's mode.
        """
        with self._lock:
            previous = self._snapshot.mode
            if previous is mode:
                return
            if self._snapshot.state is State.BUSY:
                raise SessionError("Finish the current flight before switching mode.")
            if self._session_open_locked():
                was, to = str(previous).capitalize(), str(mode).capitalize()
                raise SessionError(
                    f"A {was} session is open. It stays {was} until it ends — "
                    f"end it, then start a {to} session.")
        self._set(mode=mode)
        self._refresh_processing()
        if self.history is not None:
            self.history.set_mode(str(mode))
        if self.audit is not None:
            self.audit.record(Action.MODE_CHANGED, detail={"from": str(previous), "to": str(mode)},
                              session_id=self._snapshot.session_id)

    def _session_open_locked(self) -> bool:
        """A session exists: starting, checking, waiting, flying, ending — or
        open after failed checks (Retry keeps it). A start whose checks failed
        before a session was opened is not one."""
        return self._snapshot.session_id is not None or self._snapshot.state in (
            State.STARTING, State.AWAITING_CONFIRMATION, State.READY, State.BUSY,
            State.ENDING)

    # ── the data pipeline (the DPP switch) ───────────────────────────────

    def processing_on(self) -> bool:
        """Whether the next flight is processed: the operator's choice, kept
        until the session ends, else on — in Auto and in Manual alike (the
        owner, 2026-10-09: "by default the DPP toggle should be on")."""
        if self._processing_choice is not None:
            return self._processing_choice
        return True

    def _refresh_processing(self) -> None:
        self._set(processing={"on": self.processing_on(),
                              "chosen": self._processing_choice is not None,
                              "jobs": self.processing.jobs(),
                              "last_flight_id": self._last_flight_id,
                              "live": {"flight_id": self._live_flight_id,
                                       "points": dict(self._live_points)}})

    def set_processing(self, on: bool) -> None:
        """Turn the DPP on or off for this session. Takes effect from the next
        flight to begin; a flight in the air keeps what it began with."""
        _, audit = self._require_operator()
        self._processing_choice = bool(on)
        self._refresh_processing()
        audit.record(Action.PROCESSING_SET, session_id=self._snapshot.session_id,
                     detail={"on": bool(on), "mode": str(self._snapshot.mode)})

    # ── the camera (the auto-capture switch) ─────────────────────────────

    def capture_auto(self) -> bool:
        """Whether the camera's frames are recorded automatically: the
        operator's choice, kept until the session ends, else on. Read by the
        recording on every frame, so a change takes effect at once. Never in a
        capture session: it keeps only what is captured by hand."""
        if self.capturing_only:
            return False
        return self._capture_choice if self._capture_choice is not None else True

    def set_capture_auto(self, on: bool) -> None:
        """Turn auto-capture on or off — before a session or during one, in
        Auto and in Manual alike. Off keeps the camera live and saves only
        manual captures (2026-10-24, Samuel)."""
        _, audit = self._require_operator()
        if self.capturing_only:
            raise SessionError("A capture session keeps only the pictures you capture by "
                               "hand. Auto-capture is for sessions started on Control.")
        self._capture_choice = bool(on)
        self._set(capture={"auto": bool(on), "chosen": True})
        audit.record(Action.CAPTURE_SET, session_id=self._snapshot.session_id,
                     detail={"auto": bool(on), "mode": str(self._snapshot.mode)})

    def record_capture(self, seq: int) -> None:
        """A frame was captured by hand: on the audit trail, against the
        session it belongs to. Never fails the capture — it is already saved."""
        if self.audit is not None:
            self.audit.record(Action.CAMERA_CAPTURE, session_id=self._snapshot.session_id,
                              detail={"seq": seq, "mode": str(self._snapshot.mode)})

    def process_flight(self, flight_id: str) -> dict[str, Any]:
        """Process one recorded flight now, whatever the switch said when it
        flew — "Process this flight" on a flight recorded with DPP off."""
        _, audit = self._require_operator()
        if self._flight_id == flight_id:
            raise SessionError("That flight is still recording. Process it once it lands.")
        try:
            job = self.processing.submit(flight_id, on_change=self._processing_changed)
        except (ValueError, RuntimeError) as e:
            raise SessionError(str(e)) from None
        audit.record(Action.PROCESSING_RUN, session_id=self._snapshot.session_id,
                     flight_id=flight_id)
        return job.to_dict()

    def resume_processing(self) -> list[str]:
        """Process again every flight whose processing the last run of the
        app left unfinished (history.interrupted_flights) — in the background,
        one at a time, at low priority, like any flight that lands. Called once
        as the agent starts. Returns the flights queued."""
        try:
            flights = interrupted_flights()
            sessions = interrupted_sessions()
        except OSError:
            log.exception("could not look for unfinished processing")
            return []
        queued = []
        for flight_id in flights:
            try:
                self.processing.submit(flight_id, on_change=self._processing_changed)
                queued.append(flight_id)
            except (ValueError, RuntimeError) as e:
                log.warning("could not resume processing flight %s: %s", flight_id, e)
        # A session goes after its flights: the web shows them together.
        for session_id in sessions:
            try:
                self.processing.submit(session_id, on_change=self._processing_changed,
                                       kind=SESSION)
                queued.append(session_id)
            except (ValueError, RuntimeError) as e:
                log.warning("could not resume processing session %s: %s", session_id, e)
        if queued:
            log.info("resuming processing of %d flight(s) left unfinished", len(queued))
        return queued

    def _processing_changed(self, job: Job) -> None:
        """A job moved: the flight's line in its session history (or the
        session's own, for a session job), the app."""
        if job.kind == LIVE:
            self._live_changed(job)
            return
        history = self.history
        try:
            if job.kind == SESSION:
                set_session_processing(job.flight_id, job.state, job.error)
            elif history is not None and history.has_flight(job.flight_id):
                history.flight_processing(job.flight_id, job.state, job.error)
            else:
                set_flight_processing(job.flight_id, job.state, job.error)
        except OSError:
            log.exception("could not record the processing state of %s", job.flight_id)
        self._refresh_processing()
        self._emit("processing", job.to_dict())
        # The finished result was queued for the web (cli._queue_result_upload).
        if job.state == JobState.DONE and self._syncer is not None:
            self._syncer.trigger()

    # ── worker ───────────────────────────────────────────────────────────

    def _start_worker(self, name: str, work: Callable[[], None]) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                # Name what is running and for how long. "Something is already
                # running" was true and useless on 2026-09-22: a connect that
                # could not time out held this for ever, and the operator was
                # told to wait for a thing the message would not name.
                busy, since = self._worker_name, self._worker_started_at
                waited = f" for {time.monotonic() - since:.0f}s" if since else ""
                raise SessionError(
                    f"{busy or 'Something'} is still running{waited}. Wait for it to "
                    f"finish, or press End session to stop it."
                )

            def run() -> None:
                try:
                    work()
                except SessionError as e:
                    self._set(message=str(e))
                except Exception:
                    log.exception("%s failed", name)
                    self._set(message="Something went wrong. The log has the details.")

            self._worker_name = name.replace("_", " ").capitalize()
            self._worker_started_at = time.monotonic()
            self._worker = threading.Thread(target=run, daemon=True, name=f"session-{name}")
            self._worker.start()

    def wait_idle(self, timeout: float = 30.0) -> None:
        """For tests and for End session: wait for the current activity."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)

    # ── start: connect and check ─────────────────────────────────────────

    def start(self, purpose: str = FLIGHT) -> None:
        """Start a session — for flying (Control, every check) or for taking
        pictures by hand (the Capture page, CAPTURE_PURPOSE: the radio and the
        camera's deck only, and nothing flies)."""
        if purpose not in PURPOSES:
            raise SessionError(f"A session is for {' or '.join(PURPOSES)}, not {purpose!r}.")
        self._require_operator()
        with self._lock:
            if self._snapshot.state not in (State.IDLE, State.CHECKS_FAILED):
                raise SessionError("A session is already running.")
            if self._snapshot.session_id is not None:
                raise SessionError("This session is still open. Press Retry to check the "
                                   "drone again, or end the session first.")
        self._set(state=State.STARTING, checks=[], message=None, health_test=None,
                  purpose=purpose)
        self._start_worker("start", self._do_start)

    @property
    def capturing_only(self) -> bool:
        """A capture session: pictures by hand, nothing flies."""
        return self._snapshot.purpose == CAPTURE_PURPOSE

    def _do_start(self) -> None:
        operator, audit = self._require_operator()
        session_id = new_id()
        report = self._connect_and_check(audit, on_refused=lambda detail: audit.record(
            Action.SESSION_START, Result.REFUSED, detail=detail))
        if report is None:
            return
        link = self.link
        assert link is not None

        self.report = report
        self._outbox.put(Kind.DRONE, report.hardware_id, {
            "hardware_id": report.hardware_id, "name": report.hardware_id, "uri": link.uri,
        })
        self._outbox.put(Kind.SESSION, session_id, {
            "id": session_id, "drone_hardware_id": report.hardware_id,
            "operator_id": operator.id, "agent_id": self._agent_id,
            "mode_at_start": str(self.snapshot().mode),
            "started_at": datetime.now(UTC).isoformat(), "ended_at": None,
        })
        audit.record(Action.SESSION_START, session_id=session_id,
                     drone_hardware_id=report.hardware_id,
                     detail={"purpose": self._snapshot.purpose})
        self._height_reference = report.ground_z_m if report.assisted else None
        try:
            self.history = SessionLog(
                SessionMeta(
                    id=session_id, operator_id=operator.id, operator_email=operator.email,
                    operator_name=operator.full_name, drone_hardware_id=report.hardware_id,
                    mode=str(self.snapshot().mode), assisted=report.assisted,
                    unassisted_reason=None if report.assisted else report.unassisted_reason,
                    purpose=self._snapshot.purpose,
                    started_at=datetime.now(UTC).isoformat(),
                ),
                height_reference=lambda: self._height_reference,
            )
            # Its 1 Hz vitals go up too (sync/samples.py → session_samples):
            # a session with no flight is still a record on the web.
            self._outbox.put(Kind.SAMPLES, session_id, {
                "session_id": session_id, "folder": str(self.history.folder),
                "uploaded_through": 0, "ended": False,
            })
        except OSError:
            log.exception("could not open the session history; flying without it")
            self.history = None
        if self._on_session_open is not None:
            folder = (self.history.folder if self.history is not None
                      else sessions_dir() / session_id)
            try:
                self._on_session_open(session_id, folder)
            except Exception:
                log.exception("session-open hook failed; the session carries on")
        if self.capturing_only:
            # Nothing to confirm: confirming the area is for flying.
            self._set(state=State.READY, session_id=session_id,
                      drone={"hardware_id": report.hardware_id,
                             "battery_v": round(report.vbat, 2), "endurance_s": 0},
                      assisted=True, unassisted_reason=None, ai_deck=report.ai_deck,
                      message=CAPTURE_OPEN)
        else:
            self._set(
                state=State.AWAITING_CONFIRMATION, session_id=session_id,
                drone={"hardware_id": report.hardware_id, "battery_v": round(report.vbat, 2),
                       "endurance_s": round(report.endurance_s)},
                assisted=report.assisted,
                unassisted_reason=report.unassisted_reason,
                ai_deck=report.ai_deck,
                message=(
                    "Put the drone on a flat, clear surface, then confirm."
                    if report.assisted else
                    "The drone cannot hold a height by itself right now. Put it on a flat, "
                    "clear surface, then confirm to fly it by hand."
                ),
            )
        if self._syncer is not None:
            self._syncer.trigger()

    def _connect_and_check(
        self, audit: AuditLog, *, on_refused: Callable[[dict[str, Any]], object]
    ) -> ReadyReport | None:
        """Open the link if it is not open, then run every check.

        The one path a new session and a retry both take — so a retry is the
        same steps as starting afresh, not a shortcut past them. On any failure
        the link is closed (a power-cycled drone needs a fresh one) and the
        state says why; the caller gets None.
        """
        steps: list[dict[str, Any]] = []

        def on_step(step: CheckResult) -> None:
            nonlocal steps
            steps = [s for s in steps if s["key"] != str(step.key)] + [step.to_dict()]
            self._set(checks=steps)

        with self._link_lock:
            link = self.link
            if link is None or not link.is_open:
                # No standby link to take over: open one. (With standby
                # running, the session usually inherits the link it opened.)
                try:
                    link = self._open_link_locked()
                except LinkError as e:
                    on_refused({"reason": str(e)})
                    self._set(state=State.CHECKS_FAILED, message=str(e),
                              radio={"state": "searching", "hardware_id": None,
                                     "message": str(e)})
                    return None

        try:
            # A capture session checks only what pictures need (checks.py
            # run_capture_checks): a drone that cannot arm can still capture.
            report = collect(link.capture_checks() if self.capturing_only else link.checks(),
                             on_step)
        except ChecksFailed as e:
            audit.record(Action.CHECKS, Result.REFUSED, session_id=self._snapshot.session_id,
                         detail={"check": str(e.result.key), "reason": e.result.detail})
            self._close_link()
            self._set(state=State.CHECKS_FAILED, message=e.result.detail)
            return None

        # The geofence is centred on the Lighthouse origin; a drone parked
        # outside it would be landed by the guard the moment it took off.
        # Unassisted there is no origin and no fence — those coordinates are the
        # accelerometer integrating — so there is nothing to compare against.
        x, y = report.takeoff_xy
        if not self.capturing_only and report.assisted and max(abs(x), abs(y)) > self._fence:
            message = (
                f"The drone is at ({x:+.2f}, {y:+.2f}) m, outside the {self._fence:.1f} m "
                f"flight area. Move it nearer the middle of the room."
            )
            audit.record(Action.CHECKS, Result.REFUSED, session_id=self._snapshot.session_id,
                         detail={"reason": "outside_fence"})
            self._close_link()
            self._set(state=State.CHECKS_FAILED, message=message)
            return None
        self._set(radio={"state": "connected", "hardware_id": report.hardware_id,
                         "message": None})
        self._link_ready(link, report.ai_deck)
        return report

    def _open_link_locked(self) -> DroneLink:
        """Open a fresh link and stream its telemetry. `_link_lock` held."""
        link = self._link_factory()
        link.open()
        self.link = link
        # Live telemetry reaches the app's windows from the moment the link is
        # open — before, during and after a flight.
        if link.stream is not None:
            self._unsubscribe_telemetry = link.stream.subscribe(self._publish_telemetry)
        return link

    def _link_ready(self, link: DroneLink, ai_deck: bool | None) -> None:
        """Hand the link to the Wi-Fi hand-off, once per link."""
        hook = self._on_link_ready
        if ai_deck is False and self._on_link_no_deck is not None \
                and self._link_ready_for != id(link):
            self._link_ready_for = id(link)
            try:
                self._on_link_no_deck()
            except Exception:
                log.exception("no-deck hook failed")
            return
        if hook is None or not ai_deck or self._link_ready_for == id(link):
            return
        camera_cf = getattr(link, "camera_cf", None)
        cf = camera_cf() if camera_cf is not None else None
        if cf is None:
            return
        self._link_ready_for = id(link)

        def run() -> None:
            try:
                hook(cf)
            except Exception:
                log.exception("link-ready hook failed")

        threading.Thread(target=run, name="link-ready", daemon=True).start()

    # ── retry ────────────────────────────────────────────────────────────

    def retry(self) -> None:
        """Check the drone again in this same session, as if it were new.

        After a crash: recover the motors, run every check, ask for the area to
        be confirmed again. The session, its history and its audit trail carry
        on — the operator does not lose the record of what happened.
        """
        self._require_operator()
        with self._lock:
            snap = self._snapshot
            if snap.session_id is None:
                raise SessionError("There is no session to retry. Start one.")
            if snap.state not in (State.READY, State.CHECKS_FAILED, State.AWAITING_CONFIRMATION):
                raise SessionError("Wait for the drone to land before retrying.")
        self._set(state=State.STARTING, checks=[], health_test=None, flight=None,
                  message="Checking the drone again, as for a new session.")
        self._start_worker("retry", self._do_retry)

    def _do_retry(self) -> None:
        _, audit = self._require_operator()
        session_id = self._snapshot.session_id
        was_required = self._snapshot.retry_required
        audit.record(Action.SESSION_RETRY, session_id=session_id,
                     detail={"after_abnormal_end": was_required})
        report = self._connect_and_check(audit, on_refused=lambda detail: audit.record(
            Action.SESSION_RETRY, Result.REFUSED, session_id=session_id, detail=detail))
        if report is None:
            return                                  # still retry_required; Retry again
        self._checked_again(report, "Checked again.")

    def _checked_again(self, report: ReadyReport, lead: str) -> None:
        """The checks passed again in this session: back to confirming the area,
        exactly where a new session stands after its first checks — or, in a
        capture session, straight back to capturing."""
        self.report = report
        if self.capturing_only:
            self._set(state=State.READY, retry_required=False, activity=None,
                      drone={"hardware_id": report.hardware_id,
                             "battery_v": round(report.vbat, 2), "endurance_s": 0},
                      assisted=True, unassisted_reason=None, ai_deck=report.ai_deck,
                      message=f"{lead} {CAPTURE_OPEN}")
            return
        self._height_reference = report.ground_z_m if report.assisted else None
        if self.history is not None:
            self.history.set_positioning(report.assisted, report.unassisted_reason)
        self._set(
            state=State.AWAITING_CONFIRMATION, retry_required=False, activity=None,
            drone={"hardware_id": report.hardware_id, "battery_v": round(report.vbat, 2),
                   "endurance_s": round(report.endurance_s)},
            assisted=report.assisted, unassisted_reason=report.unassisted_reason,
            ai_deck=report.ai_deck,
            message=(
                f"{lead} Put the drone on a flat, clear surface, then confirm."
                if report.assisted else
                f"{lead} The drone cannot hold a height by itself right now — put it "
                f"on a flat, clear surface, then confirm to fly it by hand."
            ),
        )

    # ── reset: the hard switch ───────────────────────────────────────────

    def reset_drone(self) -> None:
        """Stop everything, restart the drone, and check it as for a new session.

        The switch for after a crash. Retry re-runs the checks on the drone as
        it is; this restarts the drone first. After a tumble the firmware can
        latch the motors off, and a crash can leave an estimator, a controller
        or a deck in a state nothing but a power-on clears — deck detection
        itself only runs at power-on. A restart is the one step that returns
        all of it to how it was when the session began.

        Works from any point in a session — mid-flight included, where it stops
        the motors first and on the spot, like Emergency stop, because a
        restart drops anything in the air. The session, its history and its
        audit trail carry on, as they do after Retry. With no session open it
        is restart_drone.
        """
        _, audit = self._require_operator()
        with self._lock:
            snap = self._snapshot
            if snap.session_id is None:
                in_session = False
            elif snap.state in (State.STARTING, State.ENDING):
                raise SessionError("The drone is being checked or the session is ending. "
                                   "Wait for that to finish, then reset.")
            else:
                in_session = True
        if not in_session:
            self.restart_drone(reason="the operator pressed Reset drone")
            return

        audit.record(Action.DRONE_RESET, session_id=snap.session_id, flight_id=self._flight_id,
                     detail={"state": str(snap.state), "activity": snap.activity})
        # Motors first, now, in this thread — before anything that can wait.
        manual = self.manual
        if manual is not None:
            manual.emergency_stop()
        if self.flight is not None:
            self.flight.request_stop()
        # A program flies on the worker; it winds down once told to stop.
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(RESET_WORKER_WAIT_S)
        self._start_worker("Reset drone", self._do_reset)

    def _do_reset(self) -> None:
        _, audit = self._require_operator()
        session_id = self._snapshot.session_id
        # Close the flight before the link goes. The manual guard may already be
        # closing it — it saw "stopped" — and have taken `self.manual` already,
        # so the lock is taken whatever `self.manual` reads: it waits out a
        # close in progress, whose "Motors stopped" state must land BEFORE the
        # reset's, never after it.
        with self._finish_lock:
            manual = self.manual
            if manual is not None:
                self._finish_manual_locked(manual, None)
        self._set(state=State.STARTING, activity="resetting", checks=[], health_test=None,
                  flight=None, retry_required=False,
                  message="Restarting the drone. It will be checked as for a new session.")
        with self._link_lock:
            uri = self.link.uri if self.link is not None else DEFAULT_URI
            self._close_link()
            try:
                self._power_cycle(uri)
            except Exception as e:
                log.warning("reset: restart over the radio failed: %s", e)
                audit.record(Action.DRONE_RESET, Result.FAILED, session_id=session_id,
                             detail={"reason": "power_cycle_failed"})
                self._set(state=State.CHECKS_FAILED, activity=None, retry_required=True,
                          message="Could not restart the drone over the radio. Switch it off "
                                  "and on by hand, then press Retry.")
                return
        log.info("reset: restarted the drone over the radio")
        self._set(radio={"state": "restarting", "hardware_id": None,
                         "message": "Restarting the drone…"})

        # The drone answers again ~3.6 s after a restart (measured 2026-09-24).
        # Looking sooner starts a connect that blocks for its full timeout, so
        # wait that long, then keep trying until it answers or clearly will not.
        time.sleep(self._reset_settle_s)
        deadline = time.monotonic() + self._reset_reconnect_s
        while True:
            with self._link_lock:
                try:
                    self._open_link_locked()
                    break
                except LinkError as e:
                    error = str(e)
            if time.monotonic() >= deadline:
                audit.record(Action.DRONE_RESET, Result.FAILED, session_id=session_id,
                             detail={"reason": "no_answer_after_restart"})
                self._set(state=State.CHECKS_FAILED, activity=None, retry_required=True,
                          radio={"state": "searching", "hardware_id": None, "message": error},
                          message="The drone did not answer after restarting. Check it is "
                                  "switched on, then press Retry.")
                return
            time.sleep(RESET_RECONNECT_POLL_S)

        report = self._connect_and_check(audit, on_refused=lambda detail: audit.record(
            Action.DRONE_RESET, Result.REFUSED, session_id=session_id, detail=detail))
        if report is None:
            return                                  # the checks say why; Retry is there
        self._checked_again(report, "Restarted and checked again.")

    def _publish_telemetry(self, snap: Any) -> None:
        ground = self._height_reference
        z = snap.get("stateEstimate.z")
        self._emit("telemetry", {
            "values": dict(snap.values),
            # No height without a reference: unassisted, before the barometer
            # ground is read, the Kalman z is noise and is not shown as height.
            "height_m": None if z is None or ground is None else round(z - ground, 3),
            # Whether x and y are a place (flight_guard.position_trusted): the
            # maps and the editor show the drone only when they are.
            "positioned": position_trusted(snap),
            "at": time.time(),
        })
        history = self.history
        if history is not None:
            try:
                history.sample(snap)
            except OSError:
                log.warning("could not write a history sample")
        surveying = self._survey
        if surveying is not None:
            x, y = snap.get("stateEstimate.x"), snap.get("stateEstimate.y")
            z, mask = snap.get("stateEstimate.z"), snap.get("lighthouse.bsReceive")
            if None not in (x, y, z, mask):
                surveying[1].add(float(x), float(y), float(z), int(mask),
                                 trusted=position_trusted(snap))

    # ── confirmation ─────────────────────────────────────────────────────

    def confirm_area(self, accept_unassisted: bool = False) -> None:
        """The operator confirms the area — and, when the drone cannot help
        itself, that they are taking the height and the position on themselves.

        The second acknowledgement is not paperwork. Without a position
        estimate the drone holds nothing, the drift and altitude guards cannot
        run, and Land is a throttle ramp rather than a descent. Whoever is
        standing over the drone gets to decide that, having been told it.
        """
        _, audit = self._require_operator()
        if self.capturing_only:
            raise SessionError(CAPTURE_NO_FLIGHT)
        with self._lock:
            if self._snapshot.state is not State.AWAITING_CONFIRMATION:
                raise SessionError("Run the checks first.")
        report = self._require_report()
        if not report.assisted and not accept_unassisted:
            raise SessionError(
                "The drone cannot hold a height or a position right now, so it will "
                "only fly unassisted — you control the throttle and the drone will not "
                "catch itself. Confirm that you are flying it by eye to continue."
            )
        audit.record(Action.AREA_CONFIRMED, session_id=self._snapshot.session_id,
                     detail={"assisted": report.assisted})
        self._set(state=State.READY, message=None)

    # ── battery & motor test ─────────────────────────────────────────────

    def health_test(self) -> None:
        self._require_ready("the battery & motor test")
        self._set(state=State.BUSY, activity="health_test", message=None)
        self._start_worker("health-test", self._do_health_test)

    #: The old name, for an app build that still calls it.
    prop_test = health_test

    def _do_health_test(self) -> None:
        _, audit = self._require_operator()
        link = self._require_link()
        try:
            result = link.health_test()
        except Exception as e:
            audit.record(Action.HEALTH_TEST, Result.FAILED, session_id=self._snapshot.session_id,
                         detail={"error": type(e).__name__})
            self._set(state=State.READY, activity=None,
                      message="The battery & motor test did not report a result.")
            return
        audit.record(Action.HEALTH_TEST, Result.OK if result.ok else Result.FAILED,
                     session_id=self._snapshot.session_id, detail=result.to_dict())
        problems = []
        if result.motors.failed:
            problems.append(f"Motor(s) {', '.join(str(m) for m in result.motors.failed)} did not "
                            f"pass — check for a bent or loose propeller.")
        if result.battery is None:
            problems.append(result.battery_error or "The battery test did not report.")
        elif not result.battery.passed:
            problems.append(f"The battery sagged {result.battery.sag_v:.2f} V under load, more "
                            f"than the drone accepts — charge it or use another battery.")
        self._set(state=State.READY, activity=None, health_test=result.to_dict(),
                  message=" ".join(problems) or None)

    # ── auto: programs ───────────────────────────────────────────────────

    def run_program(
        self, height_m: float = 0.30, hold_s: float = 10.0, ambient: str = "22C"
    ) -> None:
        self._require_ready("a program")
        if self.snapshot().mode is not Mode.AUTO:
            raise SessionError("Switch to Auto to run a program.")
        # A preset program flies itself to a height and holds it. With no
        # position estimate there is no height to fly to and nothing to hold
        # with — this is the exact state that put a drone into a wall on
        # 2026-09-16. Manual stays available: there the operator is the loop.
        if not self._require_report().assisted:
            raise SessionError(
                "A program needs the drone to know where it is, and it does not right "
                "now — it would fly blind. Switch to Manual to fly it by hand, or get "
                "the base stations seen and run the checks again."
            )
        try:
            program = HoverTest(height_m=height_m, hold_s=hold_s)
        except ValueError as e:
            raise SessionError(str(e)) from None
        report = self._require_report()
        if program.duration_s() > report.budget_s():
            raise SessionError(
                f"This battery has about {report.budget_s():.0f} s of flying left, and the "
                f"program needs {program.duration_s():.0f} s. Charge or shorten the hold."
            )
        self._set(state=State.BUSY, activity="program", message=None)
        self._start_worker("program", lambda: self._do_program(program, ambient))

    def _do_program(self, program: HoverTest, ambient: str) -> None:
        _, audit = self._require_operator()
        link, report = self._require_link(), self._require_report()
        flight_id = self._begin_flight(mode=Mode.AUTO, program=program.key, ambient=ambient)

        def on_phase(event: PhaseEvent) -> None:
            self._set(flight={"id": flight_id, "phase": str(event.phase), "detail": event.detail})

        flight = link.guarded_flight(
            report, target_height_m=program.height_m,
            fence_half_extent_m=self._fence, max_height_m=self._max_height, on_phase=on_phase,
        )
        self.flight = flight
        try:
            result = run_hover_test(flight, program)
        except Exception as e:
            self._finish_flight(status="failed", error=f"{type(e).__name__}: {e}")
            audit.record(Action.PROGRAM_RUN, Result.FAILED, session_id=self._snapshot.session_id,
                         flight_id=flight_id, detail={"program": program.key})
            self._set(state=State.READY, activity=None, retry_required=True,
                      message="The flight failed. Press Retry to check the drone again "
                              "before flying.")
            return
        finally:
            self.flight = None

        self._finish_flight(
            status="completed" if result.outcome is Outcome.COMPLETED else "aborted",
            outcome=str(result.outcome), abort_reason=str(result.reason),
        )
        audit.record(
            Action.PROGRAM_RUN,
            Result.OK if result.outcome is Outcome.COMPLETED else Result.ABORTED,
            session_id=self._snapshot.session_id, flight_id=flight_id,
            detail={"program": program.key, **result.to_dict()},
        )
        completed = result.outcome is Outcome.COMPLETED
        self._set(state=State.READY, activity=None, retry_required=not completed,
                  message=result.message if completed else
                  f"{result.message} Press Retry to check the drone again before flying.")

    @property
    def outbox(self) -> Outbox:
        """The records this session writes for upload — read by the API's
        flight list to say how far a flight's upload has got."""
        return self._outbox

    # ── auto: missions ───────────────────────────────────────────────────

    @property
    def fence_half_extent_m(self) -> float:
        """The agent's default flying area — a room's outer bound until its
        coverage is measured (mission/plan/validate.py flyable_bound)."""
        return self._fence

    def mission_from_drone(self, mission_id: str) -> tuple[Mission, Room,
                                                           tuple[float, float] | None]:
        """A saved mission as it would fly from where the drone is right now —
        what the Check step shows before Start. Returns the mission (from the
        drone's position when one is reported, else as saved), its room, and
        the position used. The same from_start run_mission applies, so the
        page shows exactly what Start will be checked against."""
        plan, here = self.flying_plan(mission_id)
        return plan.mission, plan.room, here

    # ── coverage: predicted and surveyed (mission/plan/coverage.py) ─────

    def predicted_coverage(self, room_id: str) -> Prediction | None:
        """Where the measured base stations reach — over the WHOLE map (the
        agent's flying area), in the room's height band, so the green is the
        stations' real reach and not the room's own outline (2026-10-05: it
        was computed inside the fence and came out as the room's box). None
        with no drone connected or no station measured (② Position)."""
        try:
            room = self.plans.room(room_id)
        except (NotFound, PlanError) as e:
            raise SessionError(str(e)) from None
        return self._prediction(room)

    def _prediction(self, room: Room) -> Prediction | None:
        poses = self._station_poses()
        if not poses:
            return None
        canvas = Geofence.square(self._fence, z_min=room.geofence.z_min,
                                 z_max=room.geofence.z_max)
        return predict(poses, canvas)

    def manual_space(self) -> Prediction | None:
        """Where the measured base stations reach, with NO room: Manual's map
        (2026-10-24, Samuel: "detect the two base stations, then draw that room
        on the spot"). The same prediction as Auto's green (predict), over the
        agent's flying area, from just off the floor to the flight ceiling.
        None with no drone connected or no station measured (② Position)."""
        poses = self._station_poses()
        if not poses:
            return None
        cached = self._space_cache
        if cached is not None and cached[0] is poses:
            return cached[1]
        canvas = Geofence.square(self._fence, z_min=MANUAL_SPACE_FLOOR_M, z_max=self._max_height)
        prediction = predict(poses, canvas)
        self._space_cache = (poses, prediction)
        return prediction

    def _station_poses(self) -> list[Any]:
        """The stations' stored poses, read once per link (a radio round trip
        each time; the plan preview asks every 2 s). Cleared when the station
        is measured again or the link changes."""
        link = self.link
        if link is None or not link.is_open:
            return []
        cached = self._poses_cache
        if cached is not None and cached[0] is link:
            return cached[1]
        poses = list(link.station_poses())
        if poses:
            self._poses_cache = (link, poses)
        return poses

    def _flyable(self, room: Room) -> Geofence:
        """Where the drone may fly: the stations' predicted reach (the green),
        else a walked survey saved on the room, else the agent's default area.
        Read only by the plan that will fly — never by the plan as drawn."""
        prediction = self._prediction(room)
        if prediction is not None and prediction.everywhere is not None:
            return prediction.everywhere
        return flyable_bound(room, default_half_extent_m=self._fence)

    def start_survey(self, room_id: str) -> None:
        """Start measuring a room's coverage: carry the drone round its edge."""
        try:
            self.plans.room(room_id)
        except (NotFound, PlanError) as e:
            raise SessionError(str(e)) from None
        if self.link is None or not self.link.is_open:
            raise SessionError("Connect the drone first — the survey reads where it is.")
        if self.snapshot().state is State.BUSY:
            raise SessionError("Land first: the survey is done with the drone in your hands, "
                               "motors off.")
        if self.position() is None:
            raise SessionError(
                "The drone does not know where it is yet, so a survey would record nothing "
                "true. Measure it first (② Position), with the drone on "
                "the floor where the station can see it.")
        self._survey = (room_id, Survey())

    def survey_status(self) -> dict[str, Any]:
        surveying = self._survey
        if surveying is None:
            return {"active": False}
        room_id, survey = surveying
        return {"active": True, "room_id": room_id, "seen": survey.seen,
                "kept": len(survey.kept), "spots": survey.spots,
                "outline": [[round(x, 3), round(y, 3)] for x, y in survey.outline()]}

    def stop_survey(self, save: bool) -> Room | None:
        """Stop the survey; with `save`, its outline becomes the room's coverage."""
        surveying, self._survey = self._survey, None
        if surveying is None:
            raise SessionError("No survey is running.")
        if not save:
            return None
        room_id, survey = surveying
        room = self.plans.room(room_id)
        coverage = survey.coverage(z_min=room.geofence.z_min, z_max=room.geofence.z_max)
        if coverage is None:
            raise SessionError(
                f"Only {len(survey.kept)} of {survey.seen} positions had enough base stations "
                "in view — not enough to enclose an area. Check the stations are on and seen, "
                "then survey again.")
        return self.plans.save_room(room.edited(coverage=coverage))

    # ── the base station: where it is, measured from where the drone sits ─
    #
    # The drone turns a station's beams into a position only once it knows
    # where the station stands (its geometry, stored on the drone). ONE record,
    # the drone wherever it sits in the station's view (2026-10-05, Samuel: no
    # walking the drone back and forth): flight/geometry.py estimate_quick,
    # which settles IPPE's mirror by the station standing upright. That spot
    # becomes (0, 0, 0). Motors off, over whichever link is open.

    def _lighthouse_deck(self, link: Any) -> tuple[bool | None, bool | None]:
        """Is the positioning deck detected, and does its firmware need writing?

        Detected is the drone's own param (local, free). The firmware question
        reads the deck memory over the radio, so it is asked ONCE per link —
        decks only change at power-on, which is a new link — and never while
        a flight is on. None means not known."""
        cf = getattr(getattr(link, "scf", None), "cf", None)
        if cf is None:
            return None, None
        try:
            detected: bool | None = str(cf.param.get_value("deck.bcLighthouse4")) == "1"
        except Exception:                                        # noqa: BLE001
            detected = None
        cached = self._deck_fw_cache
        if cached is not None and cached[0] is link:
            return detected, cached[1]
        if not detected or self.snapshot().state is State.BUSY:
            return detected, None
        from dronedeck.drone_setup import _deck_needs_fw
        try:
            needs: bool | None = _deck_needs_fw(cf, "bcLighthouse4")
        except Exception:                                        # noqa: BLE001
            log.debug("could not read the positioning deck's memory", exc_info=True)
            needs = None
        if needs is not None:
            self._deck_fw_cache = (link, needs)
        return detected, needs

    def station_status(self) -> dict[str, Any]:
        """What the drone receives and whether it can turn it into a position."""
        link = self.link
        if link is None or not link.is_open:
            return {"connected": False, "measuring": self._measuring}
        snap = link.snapshot()
        status = assess_positioning(snap)
        variances = [v for v in status.variance_m2 if v is not None]
        detected, needs_fw = self._lighthouse_deck(link)
        blocker = first_blocker(status, deck_detected=detected, deck_needs_fw=needs_fw)
        return {
            "connected": True,
            "measuring": self._measuring,
            # The positioning deck itself, before any light (2026-10-24): a
            # deck not detected, or whose firmware is not running, reads exactly
            # like stations that are off — 0 of 4 sensors.
            "deck": {"detected": detected, "needs_fw": needs_fw},
            # The first stage that is not done, with its fix — the one rule the
            # map, the stage list and ② Position all say (position_stages.py).
            "blocker": blocker.to_dict() if blocker is not None else None,
            # The chain from light to position, stage by stage (stream.py):
            # light on the sensors → the station's data read (calibrated) →
            # sweeps decoded (received) → its place stored (measured) → in use
            # by the drone (active) → settled.
            "light_sensors": sum(1 for i in range(4) if snap.get(f"lighthouse.width{i}")),
            "calibrated": list(status.calibrated),
            "active": list(station_ids(snap.get("lighthouse.bsActive"))),
            "received": list(status.received),
            "measured": list(status.with_geometry),
            "usable": list(status.usable),
            # Rounded UP to 0.1 cm (2026-11-04), so a refused reading never
            # shows as "within 5.0 cm (under 5 needed)".
            "uncertainty_cm": (uncertainty_cm(max(variances))
                               if len(variances) == 3 else None),
            "ready": status.ready,
            # The same chain, per station (2026-10-24: two stations in the
            # lab): every station any stage mentions, named by its channel.
            "stations": self._per_station(status, snap),
        }

    @staticmethod
    def _per_station(status: Any, snap: Any) -> list[dict[str, Any]]:
        active = set(station_ids(snap.get("lighthouse.bsActive")))
        seen = sorted({*status.calibrated, *status.received, *status.with_geometry, *active})
        return [{"id": s, "channel": s + 1,
                 "calibrated": s in status.calibrated, "received": s in status.received,
                 "measured": s in status.with_geometry, "active": s in active}
                for s in seen]

    def station_markers(self) -> list[dict[str, Any]]:
        """Where each measured base station stands and which way it faces, in
        the room's metres — for the tripods on the room map (2026-10-24). The
        drone's own stored geometry, read once per link and again after each
        measure (_station_poses): the place the drone positions against."""
        out = []
        for pose in self._station_poses():
            axes = pose.axes()
            out.append({
                "id": pose.station, "channel": None if pose.station is None else pose.station + 1,
                "origin": [round(v, 3) for v in pose.origin],
                **{name: [round(v, 4) for v in vec] for name, vec in axes.items()},
            })
        return out

    def measure_station(self) -> dict[str, Any]:
        """Measure where the base station stands from where the drone sits, and
        store it on the drone. The drone's spot becomes the room's origin."""
        if self.snapshot().state is State.BUSY:
            raise SessionError("Land first: the base station is measured with the drone "
                               "on the floor, motors off.")
        link = self._require_link()
        cf = link.scf.cf
        # Every station the drone receives NOW must be in the record: storing
        # clears any station it is not given, so recording while one blinks
        # out would erase it. Refused in words instead (SweepAngles).
        before = assess_positioning(link.snapshot())
        reader = SweepAngles(cf, min_stations=max(1, len(before.received)))
        self._measuring = True
        try:
            result = estimate_quick(cf, lambda _step, _i: reader.record())
        except (TimeoutError, ValueError) as e:
            raise SessionError(str(e)) from None
        finally:
            self._measuring = False
        if not result.converged:
            raise SessionError(result.message)
        if not result.written:
            raise SessionError("The drone did not confirm it stored the base station's "
                               "place. Measure again.")
        log.info("base station geometry stored: %s", result.message)
        # A station stored before and not measured now is cleared — and must
        # be: (0, 0, 0) moved, so its old place would be in the wrong room.
        cleared = [s for s in before.with_geometry if s not in result.stations]
        self._poses_cache = None                    # the green follows the new place
        # The estimate ran on no geometry until now — metres away and
        # climbing — and a filter that far off can reject the very beams that
        # would correct it. Start it again from the stored geometry, the drone
        # still where it was measured (CLAUDE.md invariant 3: the settle is
        # then read, never slept on — station_status does).
        reset_estimator(cf)
        # A session's checks ran before this: they still say "no position"
        # until they run again (Session.retry).
        then = (" This session's checks ran before it — check again before flying."
                if self.snapshot().session_id is not None else "")
        gone = ("" if not cleared else
                " " + ", ".join(channel_name(s) for s in cleared).capitalize()
                + " was not seen from here, so its stored place is cleared: measure"
                  " again where every station can see the drone.")
        return {"message": f"{result.message} Stored on the drone; this spot is now "
                           f"(0, 0, 0).{gone}{then}",
                "stations": sorted(result.stations), "cleared": cleared}

    @property
    def measuring_station(self) -> bool:
        """A measurement is reading the drone: nothing may restart it now."""
        return self._measuring

    def forget_coverage(self, room_id: str) -> Room:
        """Drop a room's measured flyable space; the agent's default area
        stands in again. For a survey that recorded the wrong thing."""
        try:
            room = self.plans.room(room_id)
        except (NotFound, PlanError) as e:
            raise SessionError(str(e)) from None
        return self.plans.save_room(room.edited(coverage=None))

    def flying_plan(self, mission_id: str) -> tuple[FlyingPlan, tuple[float, float] | None]:
        """THE PLAN THAT WILL FLY (mission/plan/fit.py): from the drone, fitted
        to the space its position can be trusted in, validated. The Check
        step's preview and run_mission both come here, so what is shown is
        what flies. Without a reported position the plan is fitted from its
        planned start."""
        try:
            mission = self.plans.mission(mission_id)
            room = self.plans.room(mission.room_id)
        except NotFound as e:
            raise SessionError(str(e)) from None
        except PlanError as e:
            raise SessionError(f"The mission could not be read: {e}") from None
        return self.plan_for(mission, room)

    def plan_for(self, mission: Mission, room: Room
                 ) -> tuple[FlyingPlan, tuple[float, float] | None]:
        """The plan that will fly for any mission and room — a saved one, or
        the editor's unsaved draft (so the Check step's second map follows the
        first as it is edited). Same function, same rules."""
        here = self.position()
        start = (here[0], here[1]) if here is not None else None
        try:
            plan = plan_to_fly(mission, room, start=start, outer=self._flyable(room))
        except PlanError as e:
            raise SessionError(str(e)) from None
        return plan, start

    def run_mission(self, mission_id: str, ambient: str = "22C") -> None:
        """Fly a saved mission.

        Every refusal happens HERE, before anything arms: the checks and the
        confirmed area, Auto, a position estimate, a mission that validates in
        its room — and again FROM WHERE THE DRONE IS (Mission.from_start: the
        drone's position is the start, the points stay put, points after an
        end point are dropped) — a battery that covers that path, and a mission
        controller that is built. Only then are the props started.

        What is armed and handed to the mission controller is that from-start
        mission, so the controller, the plan kept with the flight and the
        pipeline all see exactly what was flown.

        The mission is flown by the mission controller through the manual
        flight system — the same 50 Hz loop, leash, guards and dead-man as
        Manual — with the room's own geofence as the guard's fence.
        """
        found = self._mission_blockers(mission_id)
        if found:
            raise SessionError(found[0].message)
        plan, _ = self.flying_plan(mission_id)
        mission, room = plan.mission, plan.room
        self._set(state=State.BUSY, activity="mission", message=None,
                  mission=self._mission_summary(mission, MissionState.IDLE, None, (), None))
        self._start_worker("mission",
                           lambda: self._do_mission(mission, room, ambient, plan.moves))

    def mission_blockers(self, mission_id: str) -> list[dict[str, str]]:
        """EVERY reason Start would refuse this mission now, in the order Start
        checks them, each with what to do — ⑤ Fly lists them all (2026-10-05,
        Samuel: say why, never "started" and then nothing). Start refuses on
        the first, so the two can never disagree."""
        return [b.to_dict() for b in self._mission_blockers(mission_id)]

    def _mission_blockers(self, mission_id: str) -> list[Blocker]:
        found: list[Blocker] = []
        try:
            self._require_ready("a mission")
        except SessionError as e:
            found.append(Blocker("session", str(e),
                                 "Start the session and confirm the area (⑤ Fly)."))
        if self.snapshot().mode is not Mode.AUTO:
            found.append(Blocker("mode", "Switch to Auto to fly a mission.",
                                 "Choose Auto in the sidebar."))
        report = self.report
        if report is not None and not report.assisted:
            found.append(Blocker(
                "position",
                "A mission needs the drone to know where it is, and it does not right now "
                "— it would fly blind. Get the base stations seen and run the checks "
                "again, or switch to Manual to fly it by hand.",
                "Measure the drone's position (② Position), then Check again."))
        locked = self._motors_held()
        if locked is not None:
            found.append(Blocker("motors", locked,
                                 "Press Reset drone: it restarts and checks itself again."))
        if self.position() is None:
            found.append(Blocker(
                "no_position",
                "The drone's position is not being reported, so the mission cannot be "
                "checked from where the drone is.",
                "Measure the drone's position (② Position)."))
            return found
        # THE PLAN THAT WILL FLY (fit.py): moved to start at the drone, fitted
        # into the space its position can be trusted in, validated — what ④
        # shows and what Start flies.
        try:
            plan, _ = self.flying_plan(mission_id)
        except SessionError as e:
            found.append(Blocker("mission", str(e), "Choose or fix the mission (① Plan)."))
            return found
        if plan.unfitted:
            found.append(Blocker(
                "unfitted",
                f"{', '.join(plan.unfitted)} cannot be brought inside the space the drone "
                f"can fly in. Move {'it' if len(plan.unfitted) == 1 else 'them'} in the plan.",
                "Edit the plan (④ Auto-correct › Edit the plan)."))
        problems = errors(list(plan.problems))
        if problems:
            more = f" ({len(problems) - 1} more.)" if len(problems) > 1 else ""
            found.append(Blocker(
                "plan",
                f"From where the drone is now, this mission is not safe to fly: "
                f"{problems[0].message}{more} Move the drone, or the plan.",
                "See ④ Auto-correct for each problem."))
        if report is not None:
            needed = plan.mission.estimated_duration_s(move_speed_m_s=MOVE_SPEED_M_S,
                                                       climb_rate_m_s=CLIMB_RATE_M_S)
            if needed > report.budget_s():
                found.append(Blocker(
                    "battery",
                    f"This battery has about {report.budget_s():.0f} s of flying left, and "
                    f"the mission, from where the drone is, needs about {needed:.0f} s. "
                    f"Charge the battery or shorten the mission.",
                    "Fit a charged battery, then Check again."))
        if not getattr(self._mission_controller, "BUILT", False):
            found.append(Blocker(
                "controller",
                "The mission controller is not built yet (docs/handoffs/sprint-1/undone/"
                "mission-controller.txt), so this mission cannot fly. Nothing was armed.",
                "Build the mission controller."))
        return found

    def _motors_held(self) -> str | None:
        """Why the drone's own supervisor will not spin the motors, or None.

        2026-10-05: after a mission landed, supervisor.info read 580 — LOCKED
        (0x0040; Bitcraze supervisor.c). Three missions were then "flown"
        with thrust 0 the whole way, the app saying started and landing. The
        firmware clears LOCKED only by a restart, so this is read before every
        mission, never assumed from the session's opening checks."""
        link = self.link
        if link is None or not link.is_open:
            return None
        value = link.snapshot().get("supervisor.info")
        if value is None:
            return None
        bits = int(value)
        if bits & motor_supervisor.IS_LOCKED:
            return ("The drone has locked its motors (its supervisor reports LOCKED — it "
                    "does after some landings), so they would not spin.")
        if bits & (motor_supervisor.IS_CRASHED | motor_supervisor.IS_TUMBLED):
            return "The drone is holding its motors after a crash or tumble."
        return None

    def _do_mission(self, mission: Mission, room: Room, ambient: str,
                    moves: tuple[Move, ...] = ()) -> None:
        _, audit = self._require_operator()
        link, report = self._require_link(), self._require_report()
        flight_id = self._begin_flight(
            mode=Mode.AUTO, program=f"mission:{mission.id}@r{mission.revision}",
            ambient=ambient)
        self._keep_plan_flown(flight_id, mission, room, moves)

        controller = link.manual(report)
        self._attach_controls(controller)
        self._height_reference = controller.ground_z
        applied = [a.to_dict() for a in getattr(link, "tuning_applied", [])]
        self._start_trace(link, controller, flight_id, applied)
        controller.arm()
        controller.start()
        self.manual = controller
        self._mission_plan = mission
        audit.record(Action.MISSION_RUN, session_id=self._snapshot.session_id,
                     flight_id=flight_id,
                     detail={"stage": "start", "mission_id": mission.id,
                             "revision": mission.revision, "name": mission.name,
                             "room_id": room.id, "points": list(mission.point_ids),
                             "tuning": applied})
        self._start_manual_guard(fence=room.geofence.contains)

        flying = self._mission_controller(mission, controller, on_event=self._on_mission_event)
        self.mission = flying
        try:
            flying.start()
        except Exception as e:
            # Nothing has left the ground: the controller refused to begin.
            # Disarm, and let the guard's watch close the flight as it would.
            log.exception("the mission controller did not start")
            self.mission = None
            controller.land()
            self._set(message=f"The mission did not start: {e}")
        self.plans.mark_flown(mission.id, mission.revision)

    def _keep_plan_flown(self, flight_id: str, mission: Mission, room: Room,
                         moves: tuple[Move, ...] = ()) -> None:
        """Copy the exact plan this flight flies into the session's folder. A
        mission edited later makes a new revision, but the result of THIS flight
        must always point at what actually flew."""
        if self.history is None:
            return
        try:
            folder = self.history.folder / "missions"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{flight_id}.json").write_text(json.dumps(
                {"flight_id": flight_id, "mission": mission.to_dict(), "room": room.to_dict(),
                 "moves": [m.to_dict() for m in moves]},
                indent=2), encoding="utf-8")
        except OSError:
            log.exception("could not keep the plan flown with the session")

    def current_point_id(self) -> str | None:
        """The inspection point being held right now, or None. Asked on every
        telemetry row and every camera frame (story 3.5)."""
        flying = self.mission
        if flying is None:
            return None
        try:
            point = flying.current_point_id
        except Exception:
            return None
        return point if isinstance(point, str) and point else None

    def _mission_summary(self, mission: Mission, state: MissionState, current: str | None,
                         completed: tuple[str, ...],
                         event: MissionEvent | None) -> dict[str, Any]:
        return {
            "id": mission.id, "name": mission.name, "revision": mission.revision,
            "room_id": mission.room_id, "points": list(mission.point_ids),
            "state": str(state), "current_point_id": current,
            "completed_point_ids": list(completed),
            "last_event": event.to_dict() if event is not None else None,
        }

    def _live_changed(self, job: Job) -> None:
        """A live job moved (story 4.9): its point's line in the app — waiting,
        then the verdict it wrote, or why it could not."""
        from dronedeck.paths import results_dir
        from dronedeck.pipeline.runner import live_path

        if job.flight_id != self._live_flight_id or job.point_id is None:
            return                      # a point of a flight that has since ended
        line: dict[str, Any] = {"state": job.state}
        if job.state == JobState.DONE:
            try:
                line.update(json.loads(live_path(results_dir(), job.flight_id,
                                                 job.point_id).read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                line.update(state=JobState.FAILED, error=f"the verdict could not be read: {e}")
        elif job.state == JobState.FAILED:
            line["error"] = job.error
        self._live_points[job.point_id] = line
        self._refresh_processing()
        self._emit("processing", {"live": {"flight_id": job.flight_id,
                                           "point_id": job.point_id, **line}})

    def _queue_live(self, point_id: str | None) -> None:
        """A point's hold is done: its verdict now, from the flight so far, if
        the flight is being processed."""
        flight_id = self._flight_id
        if not (self._flight_process and flight_id and point_id):
            return
        try:
            self.processing.submit(flight_id, kind=LIVE, point_id=point_id,
                                   on_change=self._processing_changed)
        except (ValueError, RuntimeError):
            log.exception("point %s of flight %s could not be judged in flight",
                          point_id, flight_id)

    def _on_mission_event(self, event: MissionEvent) -> None:
        """The mission controller's events → the app, and the audit trail."""
        if event.kind is EventKind.POINT_COMPLETE:
            self._queue_live(event.point_id)
        mission, flying = self._mission_plan, self.mission
        if mission is None:
            return
        state = flying.state if flying is not None else MissionState.IDLE
        summary = self._mission_summary(
            mission, state, self.current_point_id(),
            tuple(flying.completed_point_ids) if flying is not None else (), event)
        self._set(mission=summary, message=event.detail)
        self._emit("mission", summary)
        if state in TERMINAL_STATES and self.audit is not None:
            self.audit.record(
                Action.MISSION_RUN,
                Result.OK if state is MissionState.DONE else Result.ABORTED,
                session_id=self._snapshot.session_id, flight_id=self._flight_id,
                detail={"stage": "end", "mission_id": mission.id,
                        "revision": mission.revision, "state": str(state),
                        "completed": summary["completed_point_ids"], "detail": event.detail})

    # ── manual ───────────────────────────────────────────────────────────

    def arm_manual(self, ambient: str = "22C") -> None:
        self._require_ready("manual control")
        if self.snapshot().mode is not Mode.MANUAL:
            raise SessionError("Switch to Manual to fly by hand.")
        _, audit = self._require_operator()
        link, report = self._require_link(), self._require_report()

        flight_id = self._begin_flight(mode=Mode.MANUAL, program=None, ambient=ambient)
        controller = link.manual(report)
        self._attach_controls(controller)
        self._height_reference = controller.ground_z
        applied = [a.to_dict() for a in getattr(link, "tuning_applied", [])]
        self._start_trace(link, controller, flight_id, applied)
        controller.arm()
        controller.start()
        self.manual = controller
        audit.record(Action.MANUAL_ARM, session_id=self._snapshot.session_id, flight_id=flight_id,
                     detail={"tuning": applied, "assisted": report.assisted})
        self._start_manual_guard()
        self._set(state=State.BUSY, activity="manual",
                  message=(
                      "Props are turning. Hold W to rise gently."
                      if report.assisted else
                      "Unassisted: props are idling. Hold W to raise the throttle until it "
                      "lifts; let go and the throttle stays put — you hold the height."
                  ))

    def hold_manual(self, height_m: float = 0.30) -> None:
        """Rise to a height and hold it, in Manual, without holding W.

        This is NOT the Auto hover test. That one is a preset program that
        flies itself and needs a position estimate to fly to (run_program
        refuses without one). This asks the manual controller for the same
        climb the W key asks for, and then stops asking — so it works
        unassisted, on the barometer, which is the state this drone is
        usually in.

        The operator stays in the loop throughout: W and S cancel it on the
        next tick, Land still lands, and every guard and the heartbeat
        dead-man are untouched.
        """
        controller = self.manual
        if controller is None or self._snapshot.activity != "manual":
            raise SessionError("Start the motors first.")
        try:
            controller.hold_at(height_m)
        except (RuntimeError, ValueError) as e:
            raise SessionError(str(e)) from None
        self._set(message=f"Rising to {height_m:.2f} m and holding. W or S takes over.")

    def _start_trace(self, link: Any, controller: Any, flight_id: str, applied: list[dict]) -> None:
        """A 10 Hz control trace beside the flight CSV (telemetry/trace.py)."""
        stream = getattr(link, "stream", None)
        if stream is None:
            return
        try:
            day = datetime.now().strftime("%Y-%m-%d")
            path = flights_dir() / day / f"trace_{flight_id[:8]}.csv"
            tuning = ";".join(f"{a['name']}={a['before']}->{a['after']}" for a in applied)
            self._trace = flight_trace.FlightTrace(path, controller, tuning=tuning)
            self._unsubscribe_trace = flight_trace.subscribe(stream, self._trace)
        except OSError:
            log.exception("could not start the flight trace; flying without it")
            self._trace = None

    def _stop_trace(self) -> None:
        if self._unsubscribe_trace is not None:
            try:
                self._unsubscribe_trace()
            except Exception:
                log.debug("trace unsubscribe failed")
            self._unsubscribe_trace = None
        if self._trace is not None:
            self._trace.close()
            self._trace = None

    # ── the arrow keys' frame ────────────────────────────────────────────

    def _controls_dict(self) -> dict[str, Any]:
        return {**self._controls.to_dict(), "live": self._controls_live}

    def _attach_controls(self, controller: Any) -> None:
        """Tell a new flight which way the arrows move it, and listen for
        when that changes — in Manual, and in a mission the keys take over."""
        controller.set_key_frame(self._controls.key_frame, self._controls.operator)
        controller.set_frame_listener(self._frame_changed)

    def _frame_changed(self, status: FrameStatus | None) -> None:
        """The manual controller says the arrows mean something new."""
        self._controls_live = None if status is None else status.to_dict()
        self._set(controls=self._controls_dict())

    def _save_controls(self) -> None:
        try:
            self._controls_store.save(self._controls)
        except OSError:
            # Kept for this run; the next launch returns to what was saved.
            log.exception("could not save the arrow-key settings")

    def _apply_controls(self) -> None:
        self._save_controls()
        controller = self.manual
        if controller is not None:
            controller.set_key_frame(self._controls.key_frame, self._controls.operator)
        self._set(controls=self._controls_dict())

    def set_key_frame(self, frame: str) -> None:
        """Which way the arrows move the drone: away from the operator
        ("operator") or along the room's own directions ("room"). Works in
        the air — the next tick uses it."""
        try:
            chosen = KeyFrame(frame)
        except ValueError:
            raise SessionError('The arrow keys follow "operator" or "room".') from None
        self._controls = self._controls.with_frame(chosen)
        self._apply_controls()

    def mark_operator(self, *, from_drone: bool = False,
                      x: float | None = None, y: float | None = None,
                      clear: bool = False) -> None:
        """Where the operator stands — what "away from you" is measured from.

        `from_drone`: the drone's position now — carry it to your feet, or fly
        it over your head, and press "I'm here". `x`, `y`: a spot in room
        metres. `clear`: forget the mark; the takeoff spot stands in for it.
        """
        if clear:
            self._controls = self._controls.with_operator(None)
            self._apply_controls()
            return
        if from_drone:
            report = self.report
            if report is not None and not report.assisted:
                raise SessionError(
                    "Without base stations the drone has no position, so there is no spot "
                    "to mark. The arrows keep the direction it faced at takeoff.")
            here = self.position()
            if here is None:
                raise SessionError(
                    "The drone is not reporting a position. Connect it where the base "
                    "stations can see it, then press I'm here again.")
            x, y = here[0], here[1]
        if x is None or y is None:
            raise SessionError("Say where you are: from the drone, or an x and y in metres.")
        if not valid_spot(x, y):
            raise SessionError(f"({x:.2f}, {y:.2f}) m is not a spot in this room.")
        self._controls = self._controls.with_operator((x, y))
        self._apply_controls()

    def set_intent(self, keys: dict[str, Any]) -> None:
        if self.manual is None:
            return
        from dronedeck.flight.manual import Intent

        self.manual.set_intent(Intent.from_payload(keys))

    def heartbeat(self) -> None:
        if self.manual is not None:
            self.manual.heartbeat()

    def _start_manual_guard(self, fence: Callable[[float, float], bool] | None = None) -> None:
        """Manual flight is watched too — the operator steers, the guard still
        ends the flight on low battery, lost positioning or the geofence.

        A mission passes its room's geofence as `fence`; Manual keeps the
        square of the agent's default fence."""
        link, report = self._require_link(), self._require_report()
        guard = link.manual_guard(
            report, fence_half_extent_m=self._fence, max_height_m=self._max_height,
            fence=fence,
        )
        stop = threading.Event()
        self._manual_guard_stop = stop

        def watch() -> None:
            verdict_seen: Any = None
            spinning_since: float | None = None     # first tick armed/flying
            while not stop.wait(MANUAL_GUARD_PERIOD_S):
                controller = self.manual
                if controller is None:
                    return
                state = str(controller.state)
                # Whatever ended it — Land, S to the floor, the heartbeat, a
                # guard, Emergency stop — a finished flight gives the session
                # back. Before this, a tumble left the app "busy" for good:
                # no End, no mode switch, no second attempt.
                if state in ("landed", "stopped", "idle"):
                    self._finish_manual(controller, verdict_seen)
                    return
                if verdict_seen is not None or state not in ("armed", "flying"):
                    continue
                now = time.monotonic()
                spinning_since = spinning_since if spinning_since is not None else now
                snap = link.snapshot()
                dead = _motors_not_spinning(snap, now - spinning_since)
                verdict = (GuardVerdict(GuardAction.LAND, GuardReason.MOTORS_NOT_SPINNING, dead)
                           if dead else guard.check(snap, now))
                if verdict.ok:
                    continue
                verdict_seen = verdict
                if self.audit is not None:
                    self.audit.record(
                        Action.GUARD_ABORT, Result.ABORTED,
                        session_id=self._snapshot.session_id, flight_id=self._flight_id,
                        detail={"reason": str(verdict.reason), "action": str(verdict.action)},
                    )
                if verdict.action is GuardAction.STOP:
                    controller.emergency_stop()
                else:
                    controller.land()
                self._set(message=verdict.message)

        threading.Thread(target=watch, daemon=True, name="manual-guard").start()

    def _finish_manual(self, controller: Any, verdict: Any) -> None:
        """Close a manual flight that has come down, and free the session."""
        with self._finish_lock:
            self._finish_manual_locked(controller, verdict)

    def _finish_manual_locked(self, controller: Any, verdict: Any) -> None:
        with self._manual_lock:
            if self.manual is not controller:
                return                      # End session got there first
            self.manual = None
        self._frame_changed(None)           # on the ground: the arrows mean nothing
        flying, self.mission = self.mission, None
        self._mission_plan = None
        if flying is not None and flying.state not in TERMINAL_STATES:
            # The flight came down under the mission — a guard, the dead-man,
            # Land or an emergency stop. The controller sees that on its next
            # tick; this makes sure it is not left commanding a landed drone.
            try:
                flying.abort("the flight ended")
            except Exception:
                log.exception("the mission controller did not stop cleanly")
        try:
            controller.stop()
        except Exception:
            log.exception("manual loop did not stop cleanly")
        if self._manual_guard_stop is not None:
            self._manual_guard_stop.set()
            self._manual_guard_stop = None
        self._stop_trace()
        report = self.report
        if self.link is not None:
            self.link.restore_estimator()        # tuning back, Kalman back
        if report is not None:
            self._height_reference = report.ground_z_m if report.assisted else None

        stopped = str(controller.state) == "stopped"
        reason = str(verdict.reason) if verdict is not None else None
        self._finish_flight(
            status="aborted" if (verdict is not None or stopped) else "completed",
            outcome="aborted" if (verdict is not None or stopped) else "landed",
            abort_reason=reason or ("emergency_stop" if stopped else None),
        )

        if verdict is not None and verdict.reason is GuardReason.BATTERY_LOW:
            # Low battery ends the operation, not just the flight: another
            # takeoff on this charge is the thing being prevented.
            self._set(state=State.READY, activity=None)
            self.end("battery_low")
            self._set(message="The battery ran low, so the drone landed and the session ended. "
                              "Charge or swap the battery, then start a new session.")
            return
        abnormal = verdict is not None or stopped
        message = (
            verdict.message + " The flight ended early — press Retry to check the drone "
            "again before flying." if verdict is not None else
            "Motors stopped. Check the drone, then press Retry to run the checks again."
            if stopped else
            "Landed. Fly again, or end the session."
        )
        self._set(state=State.READY, activity=None, flight=None,
                  retry_required=abnormal, message=message)

    # ── land and stop ────────────────────────────────────────────────────

    def land(self) -> None:
        _, audit = self._require_operator()
        audit.record(Action.LAND, session_id=self._snapshot.session_id, flight_id=self._flight_id)
        if self.manual is not None:
            self.manual.land()
        elif self.flight is not None:
            self.flight.request_land()
        self._set(message="Landing.")

    def emergency_stop(self) -> None:
        _, audit = self._require_operator()
        audit.record(Action.EMERGENCY_STOP, Result.ABORTED,
                     session_id=self._snapshot.session_id, flight_id=self._flight_id)
        if self.manual is not None:
            self.manual.emergency_stop()
        if self.flight is not None:
            self.flight.request_stop()
        self._set(message="Motors stopped.")

    # ── end ──────────────────────────────────────────────────────────────

    def end(self, reason: str = "operator") -> None:
        with self._lock:
            if self._snapshot.state in (State.SIGNED_OUT, State.IDLE):
                return
        self._set(state=State.ENDING, activity="ending",
                  message="Landing if needed, then saving this session's data.")
        self._do_end(reason)

    def _do_end(self, reason: str) -> None:
        # Land first, whatever else fails afterwards.
        with self._manual_lock:
            manual, self.manual = self.manual, None
        if manual is not None:
            self._frame_changed(None)
        try:
            if manual is not None:
                manual.stop()                    # lands if airborne, then disarms
            elif self.flight is not None:
                self.flight.request_land()
                self.wait_idle(timeout=15.0)
        except Exception:
            log.exception("landing during End session failed — stopping the motors")
            if manual is not None:
                manual.emergency_stop()
            elif self.flight is not None:
                self.flight.request_stop()

        if self._manual_guard_stop is not None:
            self._manual_guard_stop.set()
            self._manual_guard_stop = None
        self._stop_trace()
        if manual is not None and self.link:
            self.link.restore_estimator()        # tuning back, Kalman back
        self.flight = None

        if self._flight_id is not None:
            self._finish_flight(status="completed", outcome="ended_with_session")
        # The session itself is processed too — its samples around the flights,
        # in Auto and Manual alike — when the DPP switch is on as it ends (the
        # owner, 2026-10-09: "as long as a session is started we process
        # whatever data comes"). Queued below, once its samples are closed.
        process_session: str | None = None
        if self.history is not None:
            try:
                self.history.close(reason)
            except OSError:
                log.warning("could not close the session history")
            ended_id = self.history.meta.id
            if self.processing_on():
                process_session = ended_id
            self._outbox.update(Kind.SAMPLES, ended_id, lambda p: p.__setitem__("ended", True))
            self.history = None
        if self._on_session_close is not None:
            try:
                self._on_session_close()
            except Exception:
                log.exception("session-close hook failed")
        if process_session is not None:
            try:
                self.processing.submit(process_session, on_change=self._processing_changed,
                                       kind=SESSION)
            except (ValueError, RuntimeError):
                log.exception("session %s could not be queued for processing", process_session)
        self._height_reference = None

        session_id = self._snapshot.session_id
        if session_id is not None:
            self._outbox.update(Kind.SESSION, session_id, lambda p: p.update({
                "ended_at": datetime.now(UTC).isoformat(), "end_reason": reason,
            }))
        if self.audit is not None:
            self.audit.record(Action.SESSION_END, session_id=session_id, detail={"reason": reason})

        self._close_link()
        self.report = None
        if self._syncer is not None:
            self._syncer.trigger()
        self._set(state=State.IDLE, activity=None, session_id=None, drone=None, checks=[],
                  health_test=None, flight=None, assisted=True, unassisted_reason=None,
                  retry_required=False,
                  radio=self._radio_idle(),
                  message="Session ended. Data is uploading in the background.")
        self._processing_choice = None
        self._last_flight_id = None
        self._refresh_processing()
        self._capture_choice = None
        self._set(capture={"auto": True, "chosen": False}, purpose=FLIGHT)

    def position(self) -> tuple[float, float, float] | None:
        """The drone's position now, or None when it has none it can stand
        behind (flight_guard.position_trusted) — the plan that will fly, Start,
        "I'm here" and the frames all read this, and none of them may treat an
        estimate running away with no station measured as a place."""
        link = self.link
        if link is None or not link.is_open:
            return None
        try:
            snap = link.snapshot()
        except Exception:
            return None
        if not position_trusted(snap):
            return None
        xyz = (snap.get("stateEstimate.x"), snap.get("stateEstimate.y"),
               snap.get("stateEstimate.z"))
        if any(v is None for v in xyz):
            return None
        return (float(xyz[0]), float(xyz[1]), float(xyz[2]))  # type: ignore[arg-type]

    # ── standby: the drone connected with no session ─────────────────────
    #
    # Signed in and idle, the agent holds a link to the drone so the operator
    # sees its vitals and its camera before starting anything. A standby link
    # NEVER arms: nothing here commands the motors, every flight command still
    # requires a session, and a session that takes the link over runs every
    # check exactly as if it had opened the link itself. Nothing is recorded:
    # telemetry reaches a file only through a session's history.

    def start_standby(self) -> None:
        """Begin looking for the drone. Idempotent; called once by the agent."""
        if self._standby_thread is not None:
            return
        self._standby_stop.clear()
        self._standby_thread = threading.Thread(
            target=self._standby_loop, name="standby", daemon=True)
        self._standby_thread.start()
        self._wake_standby(0)

    def stop_standby(self) -> None:
        self._standby_stop.set()
        self._standby_wake.set()

    def connect_drone(self) -> None:
        """The operator's Connect: look now, and keep looking."""
        self._require_operator()
        self._standby_paused = False
        self._set(radio={"state": "searching", "hardware_id": None,
                         "message": "Looking for the drone…"})
        self._wake_standby(0)

    def disconnect_drone(self) -> None:
        """The operator's Disconnect: release the radio until Connect.

        So another tool (a flasher, the CLI) can use it. Refused mid-session —
        End session is how a session lets go of the drone.
        """
        self._require_operator()
        if self.snapshot().state != State.IDLE:
            raise SessionError("End the session first — it is using the drone.")
        self._standby_paused = True
        self._close_link()
        self._set(radio={"state": "paused", "hardware_id": None,
                         "message": "Disconnected. The radio is free for other tools."})

    def restart_drone(self, *, reason: str) -> None:
        """Restart the drone's electronics over the radio — BETWEEN SESSIONS ONLY.

        The one supported way to make the AI deck rejoin Wi-Fi and announce its
        address again. Resetting only the deck was tried and does not work: the
        STM32's UART link to the ESP32 syncs once, at boot ("There's no support
        for re-initializing the UART transport" — cpx_uart_transport.c). A
        restart runs that boot, and standby reconnects: measured 2026-09-24 at
        4.6 s to the radio, 12.1 s to the first frame.

        Refused unless idle: a session's checks describe the drone as it is, and
        a restart in the air drops it.
        """
        self._require_operator()
        if self.snapshot().state != State.IDLE:
            raise SessionError("A session is using the drone. End it first.")
        with self._link_lock:
            uri = self.link.uri if self.link is not None else DEFAULT_URI
            self._close_link()
            self._standby_not_before = time.monotonic() + RESTART_SETTLE_S
            try:
                self._power_cycle(uri)
            except Exception as e:
                log.warning("restart over the radio failed: %s", e)
                self._set(radio={"state": "searching", "hardware_id": None,
                                 "message": "Could not restart the drone over the radio."})
                return
        log.info("restarted the drone: %s", reason)
        self._set(radio={"state": "restarting", "hardware_id": None,
                         "message": "Restarting the drone…"})
        self._wake_standby(RESTART_SETTLE_S)

    def _wake_standby(self, after_s: float) -> None:
        if after_s <= 0:
            self._standby_wake.set()
        else:
            timer = threading.Timer(after_s, self._standby_wake.set)
            timer.daemon = True
            timer.start()

    def _radio_idle(self) -> dict[str, Any]:
        if self._standby_paused:
            return {"state": "paused", "hardware_id": None,
                    "message": "Disconnected. The radio is free for other tools."}
        return {"state": "searching", "hardware_id": None, "message": "Looking for the drone…"}

    def _standby_loop(self) -> None:
        while not self._standby_stop.is_set():
            self._standby_wake.wait(STANDBY_RETRY_S)
            self._standby_wake.clear()
            if self._standby_stop.is_set():
                return
            try:
                self._standby_tick()
            except Exception:
                log.exception("standby: unexpected failure; will look again")

    def _standby_tick(self) -> None:
        """One look for the drone, when — and only when — standby may hold it."""
        snap = self.snapshot()
        if snap.state != State.IDLE or self.operator is None or self._standby_paused:
            return                                   # a session owns the radio, or no one
        if time.monotonic() < self._standby_not_before:
            return                                   # the drone is still restarting
        with self._link_lock:
            if self.link is not None and self.link.is_open:
                return                               # already connected
            if self.snapshot().state != State.IDLE:
                return                               # a session started meanwhile
            link = self._link_factory()
            try:
                link.open()
            except LinkError as e:
                self._set(radio={"state": "searching", "hardware_id": None,
                                 "message": str(e)})
                return
            self.link = link
            if link.stream is not None:
                self._unsubscribe_telemetry = link.stream.subscribe(self._publish_telemetry)
        try:
            hardware_id, ai_deck = link.identify()
        except Exception:
            log.warning("standby: could not identify the drone")
            hardware_id, ai_deck = None, None
        link.on_lost(lambda reason: self._standby_lost(link, reason))
        self._set(ai_deck=ai_deck, radio={"state": "connected", "hardware_id": hardware_id,
                                          "message": None})
        log.info("standby: connected to %s", hardware_id or link.uri)
        self._link_ready(link, ai_deck)

    def _standby_lost(self, link: DroneLink, reason: str) -> None:
        """cflib's thread: record it, and let another thread close the link."""
        def drop() -> None:
            with self._link_lock:
                if self.link is not link:
                    return                           # already replaced or closed
                if self.snapshot().state != State.IDLE:
                    # A session owns this link; its own guards handle a loss.
                    # The radio state is still worth showing.
                    self._set(radio={"state": "searching", "hardware_id": None,
                                     "message": f"The drone's radio link dropped ({reason})."})
                    return
                self._close_link()
            self._set(radio={"state": "searching", "hardware_id": None,
                             "message": f"The drone disconnected ({reason}). Looking again…"})

        threading.Thread(target=drop, name="standby-lost", daemon=True).start()

    # ── flight records ───────────────────────────────────────────────────

    def _begin_flight(self, *, mode: Mode, program: str | None, ambient: str) -> str:
        link, report = self._require_link(), self._require_report()
        try:
            ambient_c, unit = parse_ambient(ambient)
        except ValueError as e:
            raise SessionError(str(e)) from None

        flight_id = new_id()
        csv_sink = CsvSink(root=flights_dir(), prefix=f"flight_{flight_id[:8]}")
        sinks: list[Any] = [csv_sink]
        if hasattr(self._cloud, "insert_telemetry"):
            sinks.append(LiveUploadSink(self._cloud.insert_telemetry))

        stream = link.stream
        assert stream is not None
        recorder = FlightRecorder(
            stream, FanOutSink(sinks), ambient_c=ambient_c, unit=unit,
            ground_z=report.ground_z_m, flight_id=flight_id,
            point_id=self.current_point_id,
        )
        recorder.start()
        self._recorder, self._flight_id = recorder, flight_id

        self._outbox.put(Kind.FLIGHT, flight_id, {
            "id": flight_id, "session_id": self._snapshot.session_id,
            "drone_hardware_id": report.hardware_id,
            "created_by": self.operator.id if self.operator else None,
            "started_at": datetime.now(UTC).isoformat(), "ended_at": None,
            "status": "running", "mode": str(mode), "program": program,
            "temp_unit": str(unit), "ambient_start": round(ambient_c if unit is TempUnit.CELSIUS
                                                           else ambient_c * 9 / 5 + 32, 2),
            "ground_z_m": round(report.ground_z_m, 3),
            "csv_path": str(csv_sink.path), "csv_object": f"{flight_id}.csv.gz",
            "rows_written": None, "csv_uploaded": False,
        })
        self._flight_process = self.processing_on()
        self._live_flight_id, self._live_points = flight_id, {}
        if self.history is not None:
            self.history.flight_started(flight_id, str(mode), program,
                                        processing=self._flight_process)
        self._set(flight={"id": flight_id, "phase": "starting", "detail": ""})
        return flight_id

    def _finish_flight(self, *, status: str, outcome: str | None = None,
                       error: str | None = None, abort_reason: str | None = None) -> None:
        flight_id, recorder = self._flight_id, self._recorder
        self._flight_id, self._recorder = None, None
        if flight_id is None:
            return
        rows = 0
        if recorder is not None:
            recorder.stop()
            rows = recorder.rows_written
        if self.history is not None:
            self.history.flight_finished(flight_id, outcome=outcome or status,
                                         abort_reason=abort_reason)
        self._outbox.update(Kind.FLIGHT, flight_id, lambda p: p.update({
            "ended_at": datetime.now(UTC).isoformat(), "status": status,
            "outcome": outcome, "error": error, "abort_reason": abort_reason,
            "rows_written": rows,
        }))
        # Its plan as flown and control trace, so it plays back on any computer
        # (sync/flight_playback.py). The trace is already closed: the manual and
        # mission paths call _stop_trace just before this; a program flight
        # (the hover test) never starts one.
        flight_playback.queue(self._outbox, flight_id)
        if self._syncer is not None:
            self._syncer.trigger()
        self._last_flight_id = flight_id
        self._refresh_processing()
        # After the CSV is closed and the flight recorded — never before: the
        # pipeline reads the file this just finished writing.
        if self._flight_process:
            self._flight_process = False
            try:
                self.processing.submit(flight_id, on_change=self._processing_changed)
            except (ValueError, RuntimeError):
                log.exception("flight %s could not be queued for processing", flight_id)

    # ── helpers ──────────────────────────────────────────────────────────

    def _require_ready(self, what: str) -> None:
        self._require_operator()
        if self.capturing_only:
            raise SessionError(CAPTURE_NO_FLIGHT)
        with self._lock:
            state = self._snapshot.state
        if state is State.BUSY:
            raise SessionError("Something is already running. Wait for it to finish.")
        if state is not State.READY:
            raise SessionError(f"Run the checks and confirm the area before starting {what}.")
        if self._snapshot.retry_required:
            raise SessionError(
                "The last flight ended abnormally. Press Retry to check the drone again "
                f"before starting {what}."
            )

    def _require_link(self) -> DroneLink:
        if self.link is None or not self.link.is_open:
            raise SessionError("Not connected to the drone.")
        return self.link

    def _require_report(self) -> ReadyReport:
        if self.report is None:
            raise SessionError("Run the checks first.")
        return self.report

    def _close_link(self) -> None:
        with self._link_lock:
            if self._unsubscribe_telemetry is not None:
                try:
                    self._unsubscribe_telemetry()
                except Exception:
                    log.debug("telemetry unsubscribe failed")
                self._unsubscribe_telemetry = None
            if self.link is not None:
                try:
                    self.link.close()
                except Exception:
                    log.exception("closing the link failed")
                self.link = None
                self._link_ready_for = None
                if self._on_link_down is not None:
                    try:
                        self._on_link_down()
                    except Exception:
                        log.exception("link-down hook failed")
        # Whatever closed it, the radio is free: look for the drone again soon
        # (the standby loop decides whether it may — not during a session).
        self._wake_standby(STANDBY_SOON_S)
