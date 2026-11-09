"""The agent's local HTTP and WebSocket API.

This is what the desktop app talks to. It binds to **localhost by default**: it
can arm a real drone, so exposing it on a network is an explicit, deliberate
act — and even on localhost every command needs the local control token, since
any page the operator has open can reach 127.0.0.1.

Two surfaces, split by how much delay each tolerates:

  REST       commands. One request, delay is harmless.
  WebSocket  the live stream out (telemetry, session state, checks) and the
             manual-control intent in. Needs 10 Hz and cannot cross the
             internet.

What is deliberately **not** here any more: `POST /flight/mission` and
`/preflight` used to fly a drone with no authentication of any kind, and they
bypassed the checks. Flying now goes through a session, which needs the token,
a signed-in operator, passing checks and a confirmed area.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from dronedeck import history, playback
from dronedeck.api.events import EventHub
from dronedeck.api.tokens import HEADER, load_or_create_token
from dronedeck.camera import DeckStream, FrameSource, NoCamera, TestPattern
from dronedeck.camera.deck import parse_addr
from dronedeck.camera.diagnose import GAP8_SILENT, NO_CAPTURE, DeckConsole
from dronedeck.camera.rate import DeckTiming
from dronedeck.camera.recording import NotRecording, Recorder
from dronedeck.camera.wifi import DeckWifi, Phase, WifiError, follow_console
from dronedeck.drone_setup import DroneSetup, SetupError
from dronedeck.flight import basestation
from dronedeck.flight.manual import CLIMB_RATE_M_S, MAX_HEIGHT_M, MOVE_SPEED_M_S
from dronedeck.mission.plan.fit import FlyingPlan
from dronedeck.mission.plan.floorplan import DEFAULT_CLEARANCE_M, PlanError, Room
from dronedeck.mission.plan.geofence import DEFAULT_Z_MAX_M, DEFAULT_Z_MIN_M
from dronedeck.mission.plan.mission import MIN_HOLD_S, Mission
from dronedeck.mission.plan.store import InUse, NotFound
from dronedeck.mission.plan.validate import (
    errors,
    outer_bound,
    validate_mission,
    validate_room,
)
from dronedeck.paths import data_dir, results_dir
from dronedeck.processing import FLIGHT_ID
from dronedeck.session import Mode, Session, SessionError, State
from dronedeck.sync import flight_playback as playback_upload
from dronedeck.sync.cloud import CloudError, SupabaseCloud
from dronedeck.sync.outbox import Kind, Outbox
from dronedeck.sync.syncer import Syncer, close_orphaned_flights

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
#: How long a REMEMBERED deck address may go unanswered before the window is
#: told it is stale and the drone needs a restart (wifi.WifiState.remembered).
STALE_ADDRESS_S = 15.0
#: Said when the drone does not detect its AI deck and cannot be restarted for
#: it (a flight session holds it). The decks are detected when the drone boots.
NO_DECK_WORDS = ("The drone does not detect its AI deck right now, so there is no camera "
                 "to reconnect. End the session and press Reconnect camera to restart "
                 "the drone, or unplug the battery for 10 seconds.")
#: At most one recovery (ask, then rejoin) per this many seconds.
RECOVER_EVERY_S = 30.0
#: At most one camera restart of the drone per this many seconds: a deck out of
#: range will not come back by being restarted in a loop.
REJOIN_EVERY_S = 60.0
#: Joining or rejoining this long without an address: ask, and rejoin.
JOIN_STUCK_S = 45.0
DEFAULT_PORT = 8765
#: How long shutting down may take before the process exits regardless. Above
#: the longest honest landing (End session waits 15 s for a flight to land,
#: session.py) and below verify_sidecar.py's 30 s EXIT_TIMEOUT_S. Without it, a
#: session.end blocked on a dead link kept the agent — and the radio — for 9 h.
SHUTDOWN_DEADLINE_S = 20.0
#: How long uvicorn waits for open connections (the window's WebSocket) before
#: the lifespan shutdown runs. Its default is to wait forever.
GRACEFUL_SHUTDOWN_S = 5

# The desktop window itself. WebKit serves it from `tauri://localhost` on macOS
# and `http://tauri.localhost` on Windows; `localhost:1420` is `pnpm tauri dev`.
# These may send commands — and still need the control token to be obeyed.
DESKTOP_ORIGINS = (
    "tauri://localhost",
    "http://tauri.localhost",
    "http://localhost:1420",
)

# Websites allowed to *read* status, so the dashboard can say whether the agent
# is running. They may never command: a page on the internet cannot know the
# token, and these origins are not allowed to POST at all.
WEBSITE_ORIGINS = (
    "https://drone-capstone.vercel.app",
    "http://localhost:3000",
)

BUILTIN_ORIGINS = (*DESKTOP_ORIGINS, *WEBSITE_ORIGINS)


def allowed_origins() -> frozenset[str]:
    extra = os.environ.get("DRONEDECK_ALLOWED_ORIGINS", "")
    return frozenset(
        [*BUILTIN_ORIGINS, *(o.strip().rstrip("/") for o in extra.split(",") if o.strip())]
    )


# ── request models ───────────────────────────────────────────────────────


class SignInRequest(BaseModel):
    email: str
    password: str


class ModeRequest(BaseModel):
    mode: Mode


class ProgramRequest(BaseModel):
    height_m: float = Field(0.30, gt=0, le=1.0, description="metres above the floor")
    hold_s: float = Field(10.0, gt=0, le=60.0)
    ambient: str = Field("22C", description="room temperature, e.g. 74F or 22C")


class ManualRequest(BaseModel):
    ambient: str = "22C"


class HoldRequest(BaseModel):
    # The ceiling is the controller's, and it depends on whether the drone can
    # see the base stations (1.00 m assisted, 0.80 m on the barometer), so the
    # real check is there. This bound only keeps an absurd number out of the
    # flight code.
    height_m: float = Field(0.30, gt=0, le=1.0, description="metres above the floor")


class DeckWifiRequest(BaseModel):
    ssid: str = Field(..., min_length=1, max_length=64)
    #: Empty for an open network. Never logged and never returned.
    password: str = Field("", max_length=128)


class MissionRunRequest(BaseModel):
    mission_id: str = Field(..., min_length=1, max_length=64)
    ambient: str = Field("22C", description="room temperature, e.g. 74F or 22C")


class ProcessingRequest(BaseModel):
    #: Process this session's flights through the data pipeline when they land.
    on: bool


class StartRequest(BaseModel):
    #: "flight" (every check; it can fly) or "capture" (the radio and the
    #: camera's deck only; it never flies).
    purpose: Literal["flight", "capture"] = "flight"


class CaptureRequest(BaseModel):
    #: Record the camera's frames automatically. Off: only manual captures.
    auto: bool


class KeyFrameRequest(BaseModel):
    #: Which way the arrow keys move the drone: "operator" (away from you) or
    #: "room" (the room's own directions). flight/keyframe.py.
    frame: str


class OperatorSpotRequest(BaseModel):
    #: Where the operator stands. Exactly one of: the drone's position now
    #: ("I'm here"), a spot in room metres, or clear (use the takeoff spot).
    from_drone: bool = False
    x: float | None = None
    y: float | None = None
    clear: bool = False


class ConfirmRequest(BaseModel):
    #: The operator accepts flying with no base stations: height from the
    #: barometer only, no position hold, no drift or fence guard. Required only
    #: when the checks reported that assistance is unavailable.
    accept_unassisted: bool = False


# ── app state ────────────────────────────────────────────────────────────


class Agent:
    """Everything the API drives. Built once, at startup."""

    def __init__(self) -> None:
        self.hub = EventHub()
        self.outbox = Outbox()
        self.token = load_or_create_token()
        self.cloud = SupabaseCloud(
            os.environ.get("SUPABASE_URL", "").strip(),
            os.environ.get("SUPABASE_ANON_KEY", "").strip(),
        )
        self.syncer = Syncer(
            self.outbox,
            lambda: self.cloud if self.cloud.operator is not None else None,
            on_status=lambda status: self.hub.publish("sync", status.to_dict()),
        )
        self.camera: FrameSource = _camera_from_env()
        #: The Wi-Fi network the AI deck should join, given by the desktop at
        #: sign-in and sent to the drone on every link. When the deck reports
        #: its address, the camera follows it there.
        self.deck_wifi = DeckWifi(
            on_change=lambda state: self.hub.publish("camera_wifi", state.to_dict()),
            on_ip=self._deck_joined,
            joined_file=data_dir() / "deck-wifi.json",
        )
        #: What the GAP8 says each frame cost it, from the drone's console.
        self.deck_timing = DeckTiming()
        #: The deck's own console lines, and why it is silent (camera/diagnose.py).
        self.deck_console = DeckConsole()
        #: Every camera frame of a session, to disk first (camera/recording.py).
        #: Standby frames are shown, never recorded.
        self.recorder = Recorder(self.outbox, position=lambda: self.session.position(),
                                 point_id=lambda: self.session.current_point_id(),
                                 auto=lambda: self.session.capture_auto())
        if isinstance(self.camera, DeckStream):
            self.camera.add_listener(self.recorder.on_frame)
        self.session = Session(
            cloud=self.cloud, outbox=self.outbox, syncer=self.syncer,
            publish=self.hub.publish, on_link_ready=self._link_ready,
            on_link_down=self._link_down, on_link_no_deck=self._link_no_deck,
            on_session_open=self.recorder.start, on_session_close=self.recorder.stop,
        )
        #: The Set up page: installs the camera software on a drone.
        self.setup = DroneSetup(
            pause_radio=self._hold_radio, resume_radio=self._release_radio,
            publish=lambda state: self.hub.publish("setup", state),
        )

    #: Stops following the deck's console, once the link it came from closes.
    _unwatch: Callable[[], None] | None = None
    #: When the camera was last recovered (asked, rejoined) — RECOVER_EVERY_S.
    _last_recover: float = 0.0
    #: The Crazyflie of the link that is up; None when down.
    _cf: Any = None
    #: When the drone was last restarted for the camera — REJOIN_EVERY_S.
    _last_rejoin: float = 0.0

    def _link_ready(self, cf: Any) -> None:
        """A link is up (standby's or a session's): follow the deck's Wi-Fi
        reports for as long as it lasts, then hand it the network."""
        self._cf = cf
        unwatch_wifi = self.deck_wifi.watch(cf)
        # The GAP8's per-frame timings ride the same console (camera/rate.py).
        def deck_line(line: str) -> None:
            self.deck_timing.feed_line(line)
            self.deck_console.feed_line(line)
        unwatch_timing = follow_console(cf, deck_line)

        def unwatch() -> None:
            unwatch_wifi()
            unwatch_timing()
        self._unwatch = unwatch
        self.deck_wifi.apply(cf)

    def restart_for_camera(self, *, reason: str) -> bool:
        """Restart the drone over the radio so its AI deck boots, joins and says
        its address again — the one cure for a deck that cannot rejoin by
        itself (camera software older than bundle 2).

        Between sessions it is Session.restart_drone; in a CAPTURE session
        (nothing flies) it is Session.reset_drone, which checks the drone again
        as for a new capture session. Never in a flight session: a restart
        drops anything in the air. False when refused.

        RESTORED 2026-10-24 evening (Samuel: "when I press on reconnect camera
        … it does switch off and on the drone"). It was removed that morning
        on the belief that the restart unpowered the decks; on the drone that
        evening 6 radio restarts on a charged battery kept all 8 deck memories
        each time, and the deck's chips were reset by each (flight-log.txt).
        """
        if self.session.measuring_station:
            # A restart mid-measurement drops the link it is reading.
            log.info("not restarting the drone (%s): the base station is being measured",
                     reason)
            return False
        try:
            snap = self.session.snapshot()
            if snap.session_id is not None and self.session.capturing_only:
                self.session.reset_drone()
            else:
                self.session.restart_drone(reason=reason)
        except SessionError as e:
            log.info("not restarting the drone (%s): %s", reason, e)
            return False
        self._last_rejoin = time.monotonic()
        return True

    def recover_camera(self, why: str) -> str:
        """Get the camera back on the network, the least disruptive way first:

          1. ASK the deck where it is, and follow it there   (any time)
          2. REJOIN it to the network                        (any time)
          3. RESTART the drone — only when the deck cannot do 1 and 2 (camera
             software older than bundle 2): idle or in a capture session,
             never in a flight session, at most once a minute

        1 and 2 touch only the deck's Wi-Fi: not the radio link, not the flight.
        Returns DeckWifi.recover's outcome, or "restarted".
        """
        cf = self._cf
        if cf is None:
            return "no-drone"
        self._last_recover = time.monotonic()
        outcome = self.deck_wifi.recover(cf, why)
        log.info("camera recovery (%s): %s", why, outcome)
        if (outcome in ("unsupported", "needs_restart")
                and time.monotonic() - self._last_rejoin >= REJOIN_EVERY_S
                and self.restart_for_camera(reason=why)):
            return "restarted"
        return outcome

    def watchdog(self) -> None:
        """Every few seconds: is the camera off the network, or is joining
        stuck? Then recover it (recover_camera). Never in a loop."""
        while True:
            time.sleep(5.0)
            try:
                wifi = self.deck_wifi.state()
                camera = self.camera
                if (self._cf is None or not isinstance(camera, DeckStream)
                        or time.monotonic() - self._last_recover < RECOVER_EVERY_S):
                    continue
                # No AI deck detected: nothing on the drone can answer, and
                # asking every half minute only fills the radio and the log.
                if self.session.snapshot().ai_deck is False:
                    continue
                # A camera chip that is not running, or a camera that fails, is
                # not a network problem: rejoining or restarting cannot help —
                # the battery must come out (camera/diagnose.py). Restarting
                # anyway, every minute, is what the lab saw on 2026-10-24.
                found = self.deck_console.diagnose(camera.silent_connects())
                if found is not None and found.code in (GAP8_SILENT, NO_CAPTURE):
                    continue
                no_frames = (not camera.status().live
                             and camera.no_frames_for() > STALE_ADDRESS_S)
                unreachable = camera.unreachable_for() > 0
                if wifi.phase is Phase.JOINED and no_frames:
                    if unreachable:
                        self.recover_camera(f"No answer from the camera at {wifi.ip}.")
                        continue
                    # It answers and sends nothing, and the drone has not said
                    # why: asking settles nothing. A restart clears the ESP32's
                    # stuck-client state (firmware/aideck-esp) — idle or in a
                    # capture session; in a flight session the operator is told.
                    self._last_recover = time.monotonic()
                    why = (f"The camera at {wifi.ip} answers but has sent no picture for "
                           f"{camera.no_frames_for():.0f} s.")
                    if (time.monotonic() - self._last_rejoin < REJOIN_EVERY_S
                            or not self.restart_for_camera(reason=why)):
                        self.deck_wifi.needs_power_cycle(
                            f"{why} End the session and press Reconnect camera, or unplug "
                            "the battery for 10 seconds, to restart the camera.")
                elif (wifi.phase in (Phase.JOINING, Phase.RECONNECTING)
                      and self.deck_wifi.phase_age() > JOIN_STUCK_S):
                    self.recover_camera(f"Still not on {wifi.ssid} after "
                                        f"{self.deck_wifi.phase_age():.0f} s.")
                elif wifi.phase is Phase.FAILED and (
                        wifi.needs_restart or self.deck_wifi.phase_age() > RECOVER_EVERY_S):
                    self.recover_camera("The last attempt to join did not work.")
            except Exception:
                log.exception("camera watchdog failed; it keeps running")

    def _link_no_deck(self) -> None:
        """A link is up but the drone reports no AI deck: there is no camera to
        hand the network to. Say so on the Wi-Fi state, with the fix — it read
        "waiting for the drone to connect" beside a connected radio."""
        if self.deck_wifi.state().ssid is not None:
            self.deck_wifi.needs_power_cycle(NO_DECK_WORDS)

    def _link_down(self) -> None:
        self._cf = None
        unwatch, self._unwatch = self._unwatch, None
        if unwatch is not None:
            unwatch()
        self.deck_wifi.link_down()

    def _hold_radio(self) -> None:
        """Set up flashes over the radio: standby lets go of it first."""
        with contextlib.suppress(SessionError):
            self.session.disconnect_drone()

    def _release_radio(self) -> None:
        with contextlib.suppress(SessionError):
            self.session.connect_drone()

    def _deck_joined(self, ip: str) -> None:
        if isinstance(self.camera, DeckStream):
            self.camera.set_host(ip)
            self.camera.kick()


def _camera_from_env() -> FrameSource:
    """Where camera frames come from, chosen by DRONEDECK_CAMERA.

    unset   NoCamera — the default, because joining the deck's access point
            takes this laptop off its normal network, so it is opt-in.
    deck    the AI deck's Wi-Fi streamer, at DRONEDECK_DECK_ADDR
            (default 192.168.4.1:5000). The laptop must be on the deck's Wi-Fi.
    test    a generated pattern, which proves the route, the content policy
            and the window without a camera.
    """
    choice = os.environ.get("DRONEDECK_CAMERA", "").strip().lower()
    if choice == "deck":
        return DeckStream(parse_addr(os.environ.get("DRONEDECK_DECK_ADDR")))
    if choice == "test":
        return TestPattern()
    return NoCamera()


agent = Agent()


def _restore_sign_in() -> None:
    # Load the Supabase library first, off the request path: on a slow machine
    # the import is much of a first sign-in, and restoring needs it anyway.
    agent.cloud.warm()
    try:
        agent.session.restore_sign_in()
    except Exception:
        log.exception("restoring the saved sign-in failed")


def _resume_work() -> None:
    """Processing is queued first, then the display chain. The two queues
    run side by side; each chunk re-reads a session's findings, so the
    frames one rests on move to the front once its flight's result exists."""
    agent.session.resume_processing()
    agent.session.resume_display()


@asynccontextmanager
async def lifespan(api: FastAPI):
    agent.hub.bind(asyncio.get_running_loop())
    # What the last run left unfinished is finished — flights it was recording
    # when the app closed are closed (sync/syncer.py), and below, flights it was
    # processing are processed again. Off in tests (DRONEDECK_RESUME=0).
    resume = os.environ.get("DRONEDECK_RESUME", "1").strip() != "0"
    if resume:
        close_orphaned_flights(agent.outbox)
        # Flights flown before playback's upload existed get theirs queued
        # once (sync/flight_playback.py): a teammate's past flights reach
        # Supabase the first time their updated app starts.
        playback_upload.queue_past_flights(agent.outbox)
    agent.syncer.start()
    # Signing back in is a network call; the API must answer /health while it
    # happens, so it runs beside startup rather than in front of it.
    threading.Thread(target=_restore_sign_in, daemon=True, name="restore-sign-in").start()
    # Hold the drone on standby whenever someone is signed in and no session
    # runs: vitals and the camera before any session. DRONEDECK_STANDBY=0
    # turns it off — for tests, and for a developer who wants the radio free.
    if os.environ.get("DRONEDECK_STANDBY", "1").strip() != "0":
        agent.session.start_standby()
        threading.Thread(target=agent.watchdog, name="camera-watchdog", daemon=True).start()
    # Flights the last run left half-processed (the app closed first) are
    # processed again, in the background. NOT in Agent(): that is built when
    # this module is imported, and a test importing it must never start
    # processing the laptop's real flights. DRONEDECK_RESUME=0 turns it off.
    if resume:
        threading.Thread(target=_resume_work, daemon=True, name="resume-processing").start()
    log.info("agent API on %s:%d", DEFAULT_HOST, DEFAULT_PORT)
    yield
    # A SIGTERM or Ctrl+C ends the session the same way closing the app does,
    # so it is bounded the same way. Only `serve` arms it: a test's TestClient
    # runs this shutdown inside the test process, which must not be exited.
    deadline_s: float | None = getattr(api.state, "exit_deadline_s", None)
    finished = arm_exit_deadline(deadline_s) if deadline_s else None
    agent.session.stop_standby()
    try:
        agent.session.end("agent shutting down")
    except Exception:
        log.exception("could not end the session cleanly")
    agent.syncer.stop()
    agent.session.processing.close()
    agent.session.display.close()
    if finished is not None:
        finished.set()


app = FastAPI(title="DroneDeck Agent", version="0.2.0", lifespan=lifespan)


#: Refusals already logged, as (origin, reason). A browser refused a preflight
#: shows its page only "could not reach the agent" — the same words as an agent
#: that is not running — so the agent's log is the one place the difference is
#: written down. Once per origin and reason: a page retrying in a loop must not
#: bury the log, and the cap bounds what a page inventing origins can add.
_cors_refusals_logged: set[tuple[str, str]] = set()
CORS_REFUSALS_LOGGED_MAX = 50


def _log_cors_refusal(origin: str, reason: str, method: str, path: str) -> None:
    key = (origin, reason)
    if key in _cors_refusals_logged or len(_cors_refusals_logged) >= CORS_REFUSALS_LOGGED_MAX:
        return
    _cors_refusals_logged.add(key)
    log.warning("refused a cross-origin %s %s from origin %r: %s (logged once per origin)",
                method, path, origin or "(none)", reason)


class LocalCORS(BaseHTTPMiddleware):
    """CORS, with the method list decided per origin.

    The desktop window is a browser too: WebKit enforces CORS on its `fetch`,
    so a single allow-list of GET blocked the app's own commands — every button
    failed with "could not reach the agent" while the WebSocket, which CORS does
    not cover, worked fine. That is what this asymmetry is for:

      desktop origins   GET and POST — plus the control token, always
      website origins   GET only, so the dashboard can see if the agent runs
      anything else     no CORS headers at all

    Removing the method split would let any page the operator has open POST to
    the agent. It would still be refused for want of the token, but a drone is
    not the place to rely on one gate.
    """

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")
        desktop = origin in DESKTOP_ORIGINS
        known = desktop or origin in allowed_origins()
        methods = "GET, POST, OPTIONS" if desktop else "GET, OPTIONS"

        if request.method == "OPTIONS" and "access-control-request-method" in request.headers:
            requested = request.headers["access-control-request-method"].upper()
            if not known:
                _log_cors_refusal(origin, "origin not allowed", requested, request.url.path)
                return Response(status_code=400, content="Disallowed CORS origin")
            if requested not in methods:
                _log_cors_refusal(origin, "method not allowed", requested, request.url.path)
                return Response(status_code=400, content="Disallowed CORS method")
            return Response(status_code=200, headers=self._headers(origin, methods, preflight=True))

        try:
            response = await call_next(request)
        except Exception as e:
            # An unexpected error used to escape past this middleware to
            # Starlette's own 500 — which carries no CORS headers, so the
            # window's browser discarded it and the operator read only "The
            # flight agent is running but did not answer" (Intel Mac, sign-in,
            # 2026-09-25). Answer here instead: readable, and in the log.
            log.exception("unexpected error in %s %s", request.method, request.url.path)
            response = JSONResponse(status_code=500, content={"detail": (
                f"The flight agent hit an unexpected error ({type(e).__name__}). "
                "Its log has the details."
            )})
        if known:
            response.headers.update(self._headers(origin, methods, preflight=False))
        return response

    @staticmethod
    def _headers(origin: str, methods: str, *, preflight: bool) -> dict[str, str]:
        headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": methods,
            "Vary": "Origin",
        }
        if preflight:
            headers["Access-Control-Allow-Headers"] = f"content-type, {HEADER.lower()}"
            headers["Access-Control-Max-Age"] = "600"
            # Chrome's Private Network Access preflight: a public https page
            # fetching 127.0.0.1 needs this answered or it discards the reply,
            # and the /setup banner can only ever say "not running".
            headers["Access-Control-Allow-Private-Network"] = "true"
        return headers


app.add_middleware(LocalCORS)


def require_token(x_agent_token: str = Header(default="")) -> None:
    """Every command. Rejects anything that is not the app that launched us."""
    if not x_agent_token or x_agent_token != agent.token:
        raise HTTPException(
            status_code=401, detail="This client is not authorised to fly the drone."
        )


Command = Depends(require_token)


def _run(action) -> dict:
    """Turn a refusal into a 409 the app can show verbatim."""
    try:
        action()
    except SessionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return agent.session.snapshot().to_dict()


# ── read-only ────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict:
    """Liveness. No radio, no token: the setup page polls this to decide
    whether to show Download or Start."""
    return {"ok": True, "service": "dronedeck-agent", "version": "0.2.0"}


@app.get("/status")
def status() -> dict:
    """A summary safe to show without the token — no operator details."""
    snapshot = agent.session.snapshot()
    return {
        "state": str(snapshot.state),
        "mode": str(snapshot.mode),
        "signed_in": snapshot.operator is not None,
        "drone_connected": snapshot.drone is not None,
        "sync": agent.syncer.status.to_dict(),
    }


@app.get("/camera")
def camera_status() -> dict:
    """What the Camera tab shows when there is nothing to show.

    Unauthenticated, like /status and for the same reason: it carries no
    operator details and commands nothing. `deck_fitted` is the DRONE's own
    answer (deck.bcAI, read over the radio during the checks), so the tab
    reports what was asked rather than what someone typed.
    """
    status = agent.camera.status()
    snapshot = agent.session.snapshot()
    payload = {**status.to_dict(), "deck_fitted": snapshot.ai_deck,
               "decks_lost": snapshot.decks_lost}
    # How fast it really is, from both ends (camera/rate.py): what arrives
    # here, and what the GAP8 says each frame cost it. None without a deck.
    payload["rate"] = agent.camera.rate.summary() if isinstance(agent.camera, DeckStream) else None
    payload["deck_timing"] = agent.deck_timing.summary()
    # What the deck itself says, and why it is silent when it is.
    silent = agent.camera.silent_connects() if isinstance(agent.camera, DeckStream) else 0
    payload["deck"] = agent.deck_console.to_dict(silent)
    # No frames AND no radio link: the likeliest reason is the drone itself —
    # off, or its battery flat — not the network. Say that first, rather than
    # a network theory about an address the drone may no longer have.
    radio_up = snapshot.radio.get("state") == "connected"
    wifi = agent.deck_wifi.state()
    #: The agent is actively working towards a picture — the window shows a
    #: spinner and the reason, not "no signal".
    payload["connecting"] = False
    if not status.live and isinstance(agent.camera, DeckStream):
        if snapshot.radio.get("state") == "restarting":
            payload["connecting"] = True
            payload["reason"] = ("Restarting the drone so its camera rejoins and reports "
                                 "its address — about 15 s.")
        elif not radio_up:
            payload["reason"] = (
                "The drone is not connected — it may be switched off or its battery "
                "flat. Its camera streams once the drone is on and connected."
            )
        elif wifi.phase in (Phase.SENDING, Phase.JOINING, Phase.RECONNECTING):
            payload["connecting"] = True
            payload["reason"] = wifi.message
        elif (found := agent.deck_console.diagnose(silent)) is not None \
                and found.code in (GAP8_SILENT, NO_CAPTURE):
            # The drone has said why, and it is not ours to fix by retrying:
            # the operator acts (the battery), so no spinner — and the
            # watchdog does not restart it either.
            payload["connecting"] = False
            payload["reason"] = found.message
        elif wifi.phase is Phase.JOINED and \
                agent.camera.no_frames_for() > STALE_ADDRESS_S:
            # Not at that address any more. The watchdog restarts the deck so it
            # says its new one; the window says what is happening meanwhile.
            payload["connecting"] = True
            unreachable = agent.camera.unreachable_for() > 0
            if unreachable:
                payload["reason"] = (f"The camera is no longer at {wifi.ip}. Asking the "
                                     "drone where it is, then rejoining it — the drone keeps "
                                     "flying as it is.")
            else:
                payload["reason"] = (
                    "The drone's camera accepts the connection but sends nothing. "
                    + ("Restarting the drone so it starts clean — about 15 s."
                       if snapshot.state is State.IDLE else
                       "End the session to let the drone restart."))
        elif wifi.phase is Phase.JOINED:
            payload["connecting"] = True
    return payload


@app.get("/camera/recording", dependencies=[Command])
def camera_recording() -> dict:
    """The session's recording, if one is running: how many frames, the latest."""
    recording = agent.recorder.current
    return recording.summary() if recording is not None else {"recording": False}


@app.get("/camera/recording/frames", dependencies=[Command])
def camera_recording_frames(
    after: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=2000),
    source: str | None = Query(None, pattern="^(auto|manual)$"),
) -> dict:
    """Recorded frames after `after`, for the backlog strip — or, with
    source=manual, the session's manual captures (the Capture page)."""
    recording = agent.recorder.current
    frames = recording.after(after, limit, source) if recording is not None else []
    return {"frames": [f.to_dict() for f in frames]}


@app.post("/camera/capture", dependencies=[Command])
def capture_frame() -> dict:
    """Save the camera's newest frame into the session, by hand — the
    Capture page's button. Works with auto-capture off. Refused outside a
    session, and when the camera has no fresh frame (a stalled feed's last
    picture is not what the operator is looking at now)."""
    data = agent.camera.frame()          # None once the frame is older than STALE_S
    if data is None:
        status = agent.camera.status()
        raise HTTPException(status_code=409, detail=(
            "The camera has no fresh frame to capture. "
            + (status.reason or "Wait for the picture to come back, then capture again.")))
    status = agent.camera.status()
    try:
        frame = agent.recorder.capture_manual(data, agent.camera.content_type,
                                              status.width or 0, status.height or 0)
    except NotRecording as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    agent.session.record_capture(frame.seq)
    return frame.to_dict()


@app.get("/camera/recording/frame/{which}", dependencies=[Command])
def camera_recording_frame(which: str) -> Response:
    """One recorded frame — a number, or "latest" — read back FROM DISK, so
    what the window shows is what was saved. 204 when there is none."""
    recording = agent.recorder.current
    if recording is None:
        return Response(status_code=204)
    if which == "latest":
        frame = recording.latest()
    else:
        try:
            frame = recording.get(int(which))
        except ValueError:
            raise HTTPException(status_code=404, detail="No such frame.") from None
    if frame is None:
        return Response(status_code=204)
    try:
        data = recording.path(frame).read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail="That frame is gone from disk.") from None
    return Response(content=data, media_type=frame.content_type,
                    headers={"Cache-Control": "no-store"})


@app.get("/camera/wifi", dependencies=[Command])
def camera_wifi() -> dict:
    """The network the deck is set to join, and how far it got. No password."""
    return agent.deck_wifi.state().to_dict()


@app.put("/camera/wifi", dependencies=[Command])
def set_camera_wifi(body: DeckWifiRequest) -> dict:
    """Set the network the deck joins. Applied on the next drone link — or now,
    when a session already holds one."""
    try:
        state = agent.deck_wifi.configure(body.ssid, body.password)
    except WifiError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    # A link already open — a session's, or standby's — gets it now; otherwise
    # the next link does.
    # A connected drone is handed it now: a deck with bundle 2 (wifi-recovery.
    # patch) moves to it in place. An older deck that joined a network this
    # power-on answers ALREADY_APPLIED, the state says needs_restart, and the
    # watchdog restarts it between sessions — no restart when none is needed.
    cf = agent._cf
    if cf is not None and agent.session.snapshot().ai_deck:
        threading.Thread(target=agent.deck_wifi.apply, args=(cf,),
                         name="deck-wifi", daemon=True).start()
    return state.to_dict()


@app.post("/camera/wifi/rejoin", dependencies=[Command])
def rejoin_camera_wifi() -> dict:
    """The operator's "Reconnect camera": ask the deck where it is, rejoin it,
    and restart the drone when the deck can do neither (Agent.recover_camera)
    — idle or in a capture session, never in a flight session. Runs in the
    background — it can take half a minute — and the page follows the Wi-Fi
    state.

    With no AI deck detected there is nothing to ask: the drone is restarted
    straight away, since the decks are detected when it boots. Refused, with
    the fix, only when a flight session holds the drone."""
    snapshot = agent.session.snapshot()
    # The deck first: with no AI deck the camera hand-off never runs, so `_cf`
    # is None and this said "Connect the drone first" beside a connected radio
    # (2026-10-24).
    if snapshot.ai_deck is False:
        if not agent.restart_for_camera(reason="You asked to reconnect; no AI deck detected."):
            raise HTTPException(status_code=409, detail=NO_DECK_WORDS)
        return agent.deck_wifi.state().to_dict()
    if agent._cf is None:
        connected = snapshot.radio.get("state") == "connected"
        raise HTTPException(status_code=409, detail=(
            "The drone is still connecting — try again in a few seconds." if connected
            else "Connect the drone first: the camera is reached through it."))
    threading.Thread(target=agent.recover_camera, args=("You asked to reconnect.",),
                     daemon=True, name="camera-recover").start()
    return agent.deck_wifi.state().to_dict()


@app.delete("/camera/wifi", dependencies=[Command])
def forget_camera_wifi() -> dict:
    return agent.deck_wifi.forget().to_dict()


@app.get("/camera/frame")
def camera_frame() -> Response:
    """The latest frame, as an image.

    Unauthenticated ON PURPOSE, and it is the narrowest route here: it is
    read-only, it commands nothing, and the agent binds localhost. An <img> tag
    cannot send a header, so requiring the token would mean putting it in a
    query string — which the WebSocket deliberately avoids because a query
    string lands in access logs. /status is already open and carries more.

    204 when there is no camera: an empty body the window can distinguish from
    a broken route, which a 404 would not.
    """
    frame = agent.camera.frame()
    if frame is None:
        return Response(status_code=204)
    return Response(
        content=frame,
        media_type=agent.camera.content_type,
        # A live feed must never be served from cache.
        headers={"Cache-Control": "no-store"},
    )


@app.get("/session", dependencies=[Command])
def session_state() -> dict:
    return agent.session.snapshot().to_dict()


# ── auth ─────────────────────────────────────────────────────────────────


@app.post("/auth/sign-in", dependencies=[Command])
def sign_in(request: SignInRequest) -> dict:
    return _run(lambda: agent.session.sign_in(request.email, request.password))


@app.post("/auth/sign-out", dependencies=[Command])
def sign_out() -> dict:
    return _run(agent.session.sign_out)


# ── history ──────────────────────────────────────────────────────────────
#
# The operator's own sessions, read from this laptop. Token-gated like every
# command: it names people and carries their flight data.


@app.get("/history/sessions", dependencies=[Command])
def list_sessions(
    limit: int = Query(20, ge=1, le=200),
    mode: Mode | None = None,           # only sessions that used this mode
) -> list[dict]:
    return history.list_sessions(limit, mode=str(mode) if mode else None)


@app.get("/history/sessions/{session_id}", dependencies=[Command])
def read_session(session_id: str) -> dict:
    try:
        found = history.read_session(session_id)
    except ValueError:
        found = None
    if found is None:
        raise HTTPException(status_code=404, detail="No such session on this computer.")
    return found


@app.get("/history/sessions/{session_id}/samples", dependencies=[Command])
def read_samples(
    session_id: str,
    variables: str = Query(..., alias="vars", description="comma-separated variable names"),
    limit: int = Query(history.MAX_SAMPLES_RETURNED, ge=1, le=history.MAX_SAMPLES_RETURNED),
    mode: Mode | None = None,           # only readings taken in this mode
) -> list[dict]:
    names = [v.strip() for v in variables.split(",") if v.strip()]
    try:
        return history.read_samples(
            session_id, names, mode=str(mode) if mode else None, limit=limit)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such session on this computer.") from None


@app.get("/history/sessions/{session_id}/frames", dependencies=[Command])
def read_session_frames(
    session_id: str, source: str | None = Query(None, pattern="^(auto|manual)$"),
) -> dict:
    """A session's recorded frames — with source=manual, its captures made
    by hand (Sessions › a session, the Capture page after it ended)."""
    try:
        return {"frames": history.session_frames(session_id, source)}
    except ValueError:
        raise HTTPException(status_code=404, detail="No such session on this computer.") from None


@app.get("/history/sessions/{session_id}/frames/{seq}", dependencies=[Command])
def read_session_frame(session_id: str, seq: int) -> Response:
    """One recorded frame of a session, read from disk."""
    try:
        found = history.session_frame(session_id, seq)
    except ValueError:
        found = None
    if found is None:
        raise HTTPException(status_code=404, detail="No such frame on this computer.")
    path, ctype = found
    return Response(content=path.read_bytes(), media_type=ctype,
                    headers={"Cache-Control": "no-store"})


@app.get("/history/sessions/{session_id}/frames/{seq}/upscaled", dependencies=[Command])
def read_session_frame_upscaled(session_id: str, seq: int) -> Response:
    """One frame's upscaled copy (display/frames.py) — for looking at, never
    evidence. A 404 says which: not made yet ("pending") or never will be at
    this version ("failed"), so the app can show the original meanwhile."""
    try:
        found = history.session_upscaled(session_id, seq)
    except ValueError:
        raise HTTPException(status_code=404, detail="No such session on this computer.") from None
    if found.state != "ready" or found.path is None:
        message = ("Not upscaled yet: it runs once the session's processing is done."
                   if found.state == "pending"
                   else f"This frame could not be upscaled: {found.error}")
        raise HTTPException(status_code=404, detail={"state": found.state, "message": message})
    return Response(content=found.path.read_bytes(), media_type="image/webp",
                    headers={"Cache-Control": "no-store"})


def _signed_in_cloud() -> SupabaseCloud | None:
    """The cloud client when someone is signed in — what the syncer uses."""
    return agent.cloud if getattr(agent.cloud, "operator", None) is not None else None


def _upload_state(flight_id: str) -> str | None:
    """How far this laptop's upload of a flight has got, for the flight list:
    its readings (Kind.FLIGHT) and its plan and trace (Kind.PLAYBACK)."""
    outbox = agent.session.outbox
    flight = outbox.get(Kind.FLIGHT, flight_id)
    if flight is None:
        return None
    extra = outbox.get(Kind.PLAYBACK, flight_id)
    done = bool(flight.get("_sent")) and (extra is None or bool(extra.get("_sent")))
    return "uploaded" if done else "uploading"


@app.get("/history/flights", dependencies=[Command])
def list_flights(
    limit: int = Query(playback.LIST_LIMIT, ge=1, le=playback.LIST_LIMIT),
    mode: Mode | None = None,
) -> dict:
    """Every flight that can be played back: this laptop's and everyone's in
    Supabase, each once (playback.list_flights). Offline or signed out, this
    laptop's still come back, with `cloud` saying why the rest did not."""
    sessions = history.list_sessions(10_000)
    cloud = _signed_in_cloud()
    rows: list[dict] | None = None
    status, error = "signed_out", None
    if cloud is not None:
        try:
            rows = cloud.list_flights(limit, mode=str(mode) if mode else None)
            status = "ok"
        except CloudError as e:
            status, error = "error", str(e)
    flights = playback.list_flights(data_dir(), sessions, rows, _upload_state,
                                    mode=str(mode) if mode else None, limit=limit)
    return {"flights": flights, "cloud": status, "cloud_error": error}


@app.get("/history/flights/{flight_id}/playback", dependencies=[Command])
def flight_playback(flight_id: str) -> dict:
    """One recorded flight to watch again (playback.py): its samples and
    control trace on one clock, the gaps, the events in words, and — for a
    mission — the plan as flown and its room as the map draws them, with the
    mission report. From this laptop's files when it has them, else from
    Supabase (anyone's flight). 404 in words when neither has it; 503 when
    Supabase could not be reached."""
    if not FLIGHT_ID.match(flight_id):
        raise HTTPException(status_code=404, detail="That is not a flight id.")
    try:
        found = playback.load_any(data_dir(), flight_id, _signed_in_cloud)
    except playback.PlaybackError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    except CloudError as e:
        raise HTTPException(status_code=503, detail=(
            f"This flight is not on this computer, and Supabase could not be reached: {e}"
        )) from None
    room = found.room
    return {
        "flight_id": found.flight_id,
        "source": found.source,
        "started_at": found.started_at,
        "duration_s": found.duration_s,
        "temp_unit": found.temp_unit,
        "truncated": found.truncated,
        "notes": found.notes,
        # The default area stands in for a flight that kept no room, so a
        # Manual flight still plays back to scale.
        "outer": _outer(room),
        "room": _room_view(room) if room is not None else None,
        "mission": _mission_view(found.mission, room) if found.mission is not None else None,
        "moves": found.moves,
        "report": found.report,
        "samples": found.samples,
        "control": found.control,
        "gaps": found.gaps,
        "events": found.events,
    }


# ── the data pipeline's results ──────────────────────────────────────────
#
# results/<flight id>/result.json, written by `dronedeck process` — started
# by the agent when a flight flown with DPP on lands (processing.py), or by
# hand. Token-gated: a verdict is about someone's equipment.


@app.get("/flights/{flight_id}/result", dependencies=[Command])
def flight_result(flight_id: str) -> dict:
    """The pipeline's result for one flight, with where its job stands. 404 in
    words while there is no result yet — the page shows that as a state."""
    if not FLIGHT_ID.match(flight_id):
        raise HTTPException(status_code=404, detail="That is not a flight id.")
    job = agent.session.processing.job(flight_id)
    path = results_dir() / flight_id / "result.json"
    if not path.exists():
        if job is not None and job.state in ("queued", "running"):
            detail = "This flight is being processed. Its result will be here shortly."
        elif job is not None and job.state == "failed":
            detail = f"Processing this flight failed: {job.error}"
        else:
            detail = "This flight has not been processed on this computer."
        raise HTTPException(status_code=404, detail=detail)
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=500,
                            detail="The result file could not be read.") from None
    return {"flight_id": flight_id, "job": job.to_dict() if job else None, "result": result}


@app.post("/flights/{flight_id}/process", dependencies=[Command])
def process_flight(flight_id: str) -> dict:
    """Process a recorded flight now — one flown with DPP off, or again."""
    try:
        return agent.session.process_flight(flight_id)
    except SessionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None


# ── session ──────────────────────────────────────────────────────────────


@app.post("/session/processing", dependencies=[Command])
def set_processing(request: ProcessingRequest) -> dict:
    """The DPP switch: process this session's flights when they land."""
    return _run(lambda: agent.session.set_processing(request.on))


@app.post("/session/capture", dependencies=[Command])
def set_capture(request: CaptureRequest) -> dict:
    """The auto-capture switch: record the camera's frames automatically."""
    return _run(lambda: agent.session.set_capture_auto(request.auto))


@app.post("/controls/frame", dependencies=[Command])
def set_key_frame(request: KeyFrameRequest) -> dict:
    """Which way the arrow keys move the drone. Works in the air."""
    return _run(lambda: agent.session.set_key_frame(request.frame))


@app.post("/controls/operator", dependencies=[Command])
def mark_operator(request: OperatorSpotRequest) -> dict:
    """Where the operator stands — what "away from you" is measured from."""
    return _run(lambda: agent.session.mark_operator(
        from_drone=request.from_drone, x=request.x, y=request.y, clear=request.clear))


@app.post("/session/mode", dependencies=[Command])
def set_mode(request: ModeRequest) -> dict:
    return _run(lambda: agent.session.set_mode(request.mode))


@app.post("/session/start", dependencies=[Command])
def start_session(request: StartRequest | None = None) -> dict:
    """Start a session: for flying (Control, the default) or, with
    {"purpose": "capture"}, for taking pictures (the Capture page)."""
    purpose = request.purpose if request is not None else "flight"
    return _run(lambda: agent.session.start(purpose))


@app.post("/session/confirm", dependencies=[Command])
def confirm_area(request: ConfirmRequest | None = None) -> dict:
    accept = bool(request and request.accept_unassisted)
    return _run(lambda: agent.session.confirm_area(accept_unassisted=accept))


@app.post("/session/health-test", dependencies=[Command])
def health_test() -> dict:
    return _run(agent.session.health_test)


# The propeller-only test's old path, now the full battery & motor test.
app.post("/session/prop-test", dependencies=[Command])(health_test)


@app.post("/session/retry", dependencies=[Command])
def retry() -> dict:
    return _run(agent.session.retry)


@app.post("/session/reset-drone", dependencies=[Command])
def reset_drone() -> dict:
    """The hard switch: motors off, restart the drone, check it as for a new
    session (Session.reset_drone). Allowed mid-flight — it stops the motors."""
    return _run(agent.session.reset_drone)


@app.post("/session/program", dependencies=[Command])
def run_program(request: ProgramRequest) -> dict:
    return _run(lambda: agent.session.run_program(
        height_m=request.height_m, hold_s=request.hold_s, ambient=request.ambient))


@app.post("/session/mission", dependencies=[Command])
def run_mission(request: MissionRunRequest) -> dict:
    """Fly a saved mission. Every refusal comes back as a 409, before anything arms."""
    return _run(lambda: agent.session.run_mission(request.mission_id, ambient=request.ambient))


@app.post("/session/manual/arm", dependencies=[Command])
def arm_manual(request: ManualRequest) -> dict:
    return _run(lambda: agent.session.arm_manual(ambient=request.ambient))


@app.post("/session/manual/hold", dependencies=[Command])
def hold_manual(request: HoldRequest) -> dict:
    """Rise to a height and hold it, without the operator holding W."""
    return _run(lambda: agent.session.hold_manual(height_m=request.height_m))


@app.post("/session/land", dependencies=[Command])
def land() -> dict:
    return _run(agent.session.land)


@app.post("/session/emergency-stop", dependencies=[Command])
def emergency_stop() -> dict:
    """Always answers. A stop that can fail is not a stop."""
    return _run(agent.session.emergency_stop)


@app.post("/session/end", dependencies=[Command])
def end_session() -> dict:
    return _run(lambda: agent.session.end("operator"))


# ── rooms and missions ───────────────────────────────────────────────────
#
# The floor plans and missions saved on this laptop (mission/plan/store.py),
# for the Auto page's Mission step. Every route needs the control token: a plan
# decides where a drone will fly.
#
# Writes are POST, not PUT or DELETE, on purpose: the desktop window's CORS
# allows GET and POST only (LocalCORS), and widening that for a verb name is
# not worth a second method any page could try.
#
# Every room and mission comes back with its `problems` (validate.py) and
# `valid`, so the editor shows exactly what the agent will refuse — the agent's
# own check is the authority, not the editor's copy of it.


def _plan_refusal(e: Exception) -> HTTPException:
    if isinstance(e, NotFound):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, InUse):
        return HTTPException(status_code=409, detail=str(e))
    return HTTPException(status_code=422, detail=str(e))


def _outer(room: Room | None) -> dict:
    bound = outer_bound(room, default_half_extent_m=agent.session.fence_half_extent_m)
    return {"vertices": bound.to_dict()["vertices"],
            "measured": room is not None and room.coverage is not None}


def _room_view(room: Room) -> dict:
    problems = validate_room(room, outer=outer_bound(
        room, default_half_extent_m=agent.session.fence_half_extent_m))
    return {**room.to_dict(), "outer": _outer(room),
            "problems": [p.to_dict() for p in problems], "valid": not errors(problems)}


def _mission_view(mission: Mission, room: Room | None) -> dict:
    if room is None:
        problems = [{"code": "missing_room", "severity": "error", "where": None,
                     "message": f"Its room {mission.room_id} is not on this computer."}]
        valid = False
    else:
        found = validate_mission(mission, room, outer=outer_bound(
            room, default_half_extent_m=agent.session.fence_half_extent_m))
        problems, valid = [p.to_dict() for p in found], not errors(found)
    return {**mission.to_dict(), "problems": problems, "valid": valid,
            "path_length_m": round(mission.path_length_m(), 3),
            "estimated_duration_s": round(mission.estimated_duration_s(
                move_speed_m_s=MOVE_SPEED_M_S, climb_rate_m_s=CLIMB_RATE_M_S), 1)}


def _room_or_none(room_id: str) -> Room | None:
    try:
        return agent.session.plans.room(room_id)
    except (NotFound, PlanError):
        return None


@app.get("/plan/limits", dependencies=[Command])
def plan_limits() -> dict:
    """What the editor clamps every shape to, before any room exists."""
    return {
        "outer": _outer(None),
        "default_clearance_m": DEFAULT_CLEARANCE_M,
        "min_hold_s": MIN_HOLD_S,
        "z_min_m": DEFAULT_Z_MIN_M, "z_max_m": min(DEFAULT_Z_MAX_M, MAX_HEIGHT_M),
        "move_speed_m_s": MOVE_SPEED_M_S, "climb_rate_m_s": CLIMB_RATE_M_S,
    }


@app.get("/rooms", dependencies=[Command])
def list_rooms() -> list[dict]:
    return [_room_view(r) for r in agent.session.plans.rooms()]


@app.get("/rooms/{room_id}", dependencies=[Command])
def read_room(room_id: str) -> dict:
    try:
        return _room_view(agent.session.plans.room(room_id))
    except (NotFound, PlanError) as e:
        raise _plan_refusal(e) from None


@app.post("/rooms", dependencies=[Command])
def save_room(body: dict = Body(...)) -> dict:  # noqa: B008 — FastAPI's own idiom
    try:
        return _room_view(agent.session.plans.save_room(Room.from_dict(body)))
    except PlanError as e:
        raise _plan_refusal(e) from None


@app.get("/rooms/{room_id}/coverage", dependencies=[Command])
def room_coverage(room_id: str) -> dict:
    """Where the drone's position can be trusted in this room: MEASURED (the
    survey — what flies) and PREDICTED (from the base stations' poses on the
    drone — a guide, never flown on). Either may be null."""
    try:
        room = agent.session.plans.room(room_id)
        predicted = agent.session.predicted_coverage(room_id)
    except (NotFound, PlanError) as e:
        raise _plan_refusal(e) from None
    except SessionError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    return {"measured": room.coverage.to_dict() if room.coverage else None,
            "predicted": predicted.to_dict() if predicted else None,
            "survey": agent.session.survey_status()}


@app.get("/basestations", dependencies=[Command])
def base_stations() -> dict:
    """Every base station plugged into this laptop by USB, with its channel."""
    return {"stations": [s.to_dict() for s in basestation.find()]}


@app.post("/basestations/channel", dependencies=[Command])
def set_base_station_channel(body: dict = Body(...)) -> dict:  # noqa: B008 — FastAPI's own idiom
    """Set one base station's channel (Bitcraze's "Set BS channel")."""
    try:
        port, channel = str(body["port"]), int(body["channel"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Say which port and which channel.") from None
    if port not in {s.port for s in basestation.find()}:
        raise HTTPException(status_code=404,
                            detail="That base station is no longer plugged in. Scan again.")
    try:
        confirmed = basestation.set_channel(port, channel)
    except basestation.BaseStationError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return {"port": port, "channel": confirmed}


@app.post("/rooms/{room_id}/coverage/forget", dependencies=[Command])
def forget_coverage(room_id: str) -> dict:
    """Drop the room's measured flyable space; the default area stands in."""
    try:
        return _room_view(agent.session.forget_coverage(room_id))
    except SessionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None


@app.post("/rooms/{room_id}/survey/start", dependencies=[Command])
def start_survey(room_id: str) -> dict:
    try:
        agent.session.start_survey(room_id)
    except SessionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return agent.session.survey_status()


@app.get("/survey", dependencies=[Command])
def survey_status() -> dict:
    return agent.session.survey_status()


@app.post("/survey/stop", dependencies=[Command])
def stop_survey(body: dict = Body(default={})) -> dict:  # noqa: B008 — FastAPI's own idiom
    try:
        room = agent.session.stop_survey(save=bool(body.get("save", False)))
    except SessionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return {"room": _room_view(room) if room is not None else None}


@app.get("/station/poses", dependencies=[Command])
def station_poses() -> dict:
    """Each measured base station's place and facing, for the room map.
    Empty with no drone connected or nothing measured."""
    return {"stations": agent.session.station_markers()}


@app.get("/station/space", dependencies=[Command])
def station_space() -> dict:
    """Manual's room, drawn on the spot from the base stations alone: where
    their light reaches over the agent's flying area (session.manual_space).
    `predicted` is null with no drone connected or no station measured."""
    prediction = agent.session.manual_space()
    return {"predicted": prediction.to_dict() if prediction else None,
            "fence_half_extent_m": agent.session.fence_half_extent_m}


@app.get("/station", dependencies=[Command])
def station_status() -> dict:
    """The base station as the drone sees it: received, measured, usable."""
    return agent.session.station_status()


@app.post("/station/measure", dependencies=[Command])
def measure_station() -> dict:
    """Measure where the base station stands from where the drone sits, and
    store it on the drone (one record; that spot becomes the origin)."""
    try:
        return agent.session.measure_station()
    except SessionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None


@app.post("/rooms/{room_id}/delete", dependencies=[Command])
def delete_room(room_id: str) -> dict:
    try:
        agent.session.plans.delete_room(room_id)
    except (NotFound, PlanError) as e:
        raise _plan_refusal(e) from None
    return {"deleted": room_id}


@app.get("/missions", dependencies=[Command])
def list_missions() -> list[dict]:
    rooms = {r.id: r for r in agent.session.plans.rooms()}
    return [_mission_view(m, rooms.get(m.room_id)) for m in agent.session.plans.missions()]


@app.get("/missions/{mission_id}", dependencies=[Command])
def read_mission(mission_id: str) -> dict:
    try:
        mission = agent.session.plans.mission(mission_id)
    except (NotFound, PlanError) as e:
        raise _plan_refusal(e) from None
    return _mission_view(mission, _room_or_none(mission.room_id))


@app.post("/missions", dependencies=[Command])
def save_mission(body: dict = Body(...)) -> dict:  # noqa: B008
    try:
        mission = agent.session.plans.save_mission(Mission.from_dict(body))
    except PlanError as e:
        raise _plan_refusal(e) from None
    return _mission_view(mission, _room_or_none(mission.room_id))


@app.post("/missions/validate", dependencies=[Command])
def validate_draft(body: dict = Body(...)) -> dict:  # noqa: B008
    """Check a draft that has not been saved: {"mission": ..., "room": ...}.
    `room` is optional — without it, the saved room the mission names."""
    try:
        mission = Mission.from_dict(body.get("mission") or {})
        room = Room.from_dict(body["room"]) if body.get("room") else \
            agent.session.plans.room(mission.room_id)
    except (NotFound, PlanError) as e:
        raise _plan_refusal(e) from None
    return _mission_view(mission, room)


@app.get("/missions/{mission_id}/blockers", dependencies=[Command])
def mission_blockers(mission_id: str) -> dict:
    """Every reason Start would refuse this mission now, with what to do —
    the same list Start refuses on (Session.mission_blockers)."""
    return {"blockers": agent.session.mission_blockers(mission_id)}


@app.get("/missions/{mission_id}/from-drone", dependencies=[Command])
def mission_from_drone(mission_id: str) -> dict:
    """The mission checked FROM WHERE THE DRONE IS NOW — its position is the
    start, the points stay where they are, points after an end point are
    dropped. What Start will be checked against (Session.run_mission), for the
    Check step to show first. `position` is null with no drone reporting one;
    the mission then comes back as saved."""
    try:
        plan, here = agent.session.flying_plan(mission_id)
    except SessionError as e:
        raise HTTPException(status_code=404, detail=str(e)) from None
    return _flying_plan_view(plan, here)


@app.post("/missions/fit", dependencies=[Command])
def fit_draft(body: dict = Body(...)) -> dict:  # noqa: B008 — FastAPI's own idiom
    """The plan that will fly for the editor's UNSAVED draft ({mission, room}):
    the Check step's second map follows the first as it is edited. Nothing is
    saved."""
    try:
        mission = Mission.from_dict(body["mission"])
        room = Room.from_dict(body["room"])
        plan, here = agent.session.plan_for(mission, room)
    except (KeyError, TypeError, PlanError) as e:
        raise HTTPException(status_code=422, detail=f"The draft could not be read: {e}") from None
    except SessionError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    return _flying_plan_view(plan, here)


def _flying_plan_view(plan: FlyingPlan, here: tuple[float, float] | None) -> dict:
    """THE PLAN THAT WILL FLY (mission/plan/fit.py) — exactly what Start flies:
    from the drone, fitted to the space, with every point it moved."""
    return {"position": None if here is None else [round(here[0], 4), round(here[1], 4)],
            "mission": _mission_view(plan.mission, plan.room),
            "space": plan.room.geofence.to_dict(),
            "moves": [m.to_dict() for m in plan.moves],
            "unfitted": list(plan.unfitted)}


@app.post("/missions/{mission_id}/delete", dependencies=[Command])
def delete_mission(mission_id: str) -> dict:
    try:
        agent.session.plans.delete_mission(mission_id)
    except (NotFound, PlanError) as e:
        raise _plan_refusal(e) from None
    return {"deleted": mission_id}


@app.post("/drone/connect", dependencies=[Command])
def drone_connect() -> dict:
    """Look for the drone now and hold it on standby — vitals and camera with
    no session. Never arms."""
    try:
        agent.session.connect_drone()
    except SessionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return agent.session.snapshot().to_dict()


@app.post("/drone/disconnect", dependencies=[Command])
def drone_disconnect() -> dict:
    """Release the radio until Connect, so another tool can use it."""
    try:
        agent.session.disconnect_drone()
    except SessionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return agent.session.snapshot().to_dict()


@app.get("/setup", dependencies=[Command])
def setup_state() -> dict:
    """Where Set up is: what the drone has, what is installing, how far."""
    return agent.setup.state()


@app.get("/setup/command", dependencies=[Command])
def setup_command() -> dict:
    """The terminal command that installs the drone's software on THIS machine
    (dronedeck setup), for the Set up page to show and copy."""
    from dronedeck.setup_cli import command_line
    line = command_line()
    return {"command": line, "check": f"{line} --check"}


def _setup_allowed() -> None:
    if agent.session.snapshot().state is not State.IDLE:
        raise HTTPException(status_code=409, detail="End the session first — Set up "
                                                    "needs the radio to itself.")


@app.post("/setup/check", dependencies=[Command])
def setup_check() -> dict:
    """Read what the drone has installed. Changes nothing on the drone."""
    _setup_allowed()
    try:
        agent.setup.check()
    except SetupError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return agent.setup.state()


# NO INSTALL ROUTE (2026-10-24, Samuel: no button may write firmware — "they
# should copy the command and run it in the terminal"). POST /setup/install
# was removed so nothing in the app, or anything holding its token, can flash
# the drone: installing is `dronedeck setup`, run with DroneDeck quit
# (setup_cli.py). /setup and /setup/check only read.


@app.post("/sync/now", dependencies=[Command])
def sync_now() -> dict:
    agent.syncer.trigger()
    return agent.syncer.status.to_dict()


# ── the live socket ──────────────────────────────────────────────────────


@app.websocket("/ws/live")
async def live(websocket: WebSocket) -> None:
    """Session state, checks and telemetry out; manual intent and heartbeat in.

    The token arrives in the first message rather than the URL: a query string
    is written to the access log, and this one grants control of a drone.
    """
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in allowed_origins():
        log.warning("refused a live socket from origin %r", origin)
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        opening = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
    except (TimeoutError, Exception):
        await websocket.close(code=1008)
        return

    if opening.get("type") != "auth" or opening.get("token") != agent.token:
        await websocket.send_json({"type": "error", "message": "not authorised"})
        await websocket.close(code=1008)
        return

    queue = agent.hub.subscribe()
    await websocket.send_json({"type": "session", **agent.session.snapshot().to_dict()})
    await websocket.send_json({"type": "sync", **agent.syncer.status.to_dict()})

    async def pump() -> None:
        while True:
            message = await queue.get()
            await websocket.send_json(message)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")
            if kind == "intent":
                agent.session.set_intent(message.get("keys", {}))
            elif kind == "heartbeat":
                agent.session.heartbeat()
            elif kind == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        # Expected: the window closed. The manual controller's heartbeat
        # timeout lands the drone by itself.
        log.info("live socket disconnected")
    except Exception:
        log.exception("live socket failed")
    finally:
        pump_task.cancel()
        agent.hub.unsubscribe(queue)


# ── serving ──────────────────────────────────────────────────────────────


class QuietHealthFilter(logging.Filter):
    """Log the first few /health polls, then stop.

    The app polls /health every 2 s; without this the agent log is nothing but
    those lines, and the flight messages that matter scroll away.
    """

    def __init__(self, keep: int = 10) -> None:
        super().__init__()
        self._keep = keep
        self._seen = 0

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/health" not in message:
            return True
        self._seen += 1
        if self._seen == self._keep:
            log.info("further /health polls will not be logged")
        return self._seen <= self._keep


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
          exit_with_parent: bool = False) -> None:
    """Run the API. Binding beyond localhost is deliberate — see module docs."""
    import uvicorn

    if host not in {"127.0.0.1", "localhost"}:
        log.warning(
            "binding to %s — this API can arm a drone. Only do this on a trusted network.", host
        )
    if exit_with_parent:
        exit_when_parent_closes()

    app.state.exit_deadline_s = SHUTDOWN_DEADLINE_S
    logging.getLogger("uvicorn.access").addFilter(QuietHealthFilter())
    uvicorn.run(app, host=host, port=port, log_level="info",
                timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S)


def arm_exit_deadline(deadline_s: float) -> threading.Event:
    """Exit the process in `deadline_s` unless the returned event is set first.

    Shutting down tries to land the drone and end the session, and either can
    block on a link that has stopped answering. Measured 2026-09-30: the app
    closed, the parent watchdog fired, `session.end` never returned, and the
    agent held the radio and the port for 9 h — refusing SIGTERM, because the
    lifespan shutdown calls the same `session.end` — until it was killed by
    hand. The next launch and `install.mjs` both found it still running.

    The reaper takes no lock and does not wait on logging: a blocked log
    handler is one of the things that can hang. Its one line is written from a
    thread of its own, and the exit does not wait for it.
    """
    finished = threading.Event()

    def reap() -> None:
        if finished.wait(deadline_s):
            return
        threading.Thread(
            target=log.error, daemon=True, name="exit-deadline-log",
            args=("shutdown did not finish within %.0f s — exiting anyway", deadline_s),
        ).start()
        time.sleep(0.2)
        os._exit(1)

    threading.Thread(target=reap, daemon=True, name="exit-deadline").start()
    return finished


def _detach_console_logging() -> None:
    """Stop logging to stdout and stderr; the log file carries on.

    Once the parent is gone, nothing reads those pipes. The agent's handlers
    write to stderr before the file (cli.py), so a write that blocks there
    holds the handler's lock and freezes every thread that logs after it — the
    9 h agent's log stopped mid-sentence four minutes after the app closed.
    """
    consoles = {sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__}
    loggers = [logging.getLogger()] + [
        found for found in logging.Logger.manager.loggerDict.values()
        if isinstance(found, logging.Logger)
    ]
    for logger in loggers:
        for handler in list(logger.handlers):
            # Exactly StreamHandler: a FileHandler is a subclass, and it stays.
            if type(handler) is logging.StreamHandler and handler.stream in consoles:
                logger.removeHandler(handler)


def exit_when_parent_closes(deadline_s: float = SHUTDOWN_DEADLINE_S) -> None:
    """Shut down when whoever launched us goes away. Used by the desktop app.

    Killing the sidecar from the desktop side is not enough, and the reason is
    worth writing down because it is invisible and it strands an armed drone.

    A PyInstaller one-file binary is **two** processes: a bootloader that
    unpacks the archive, and the real Python process it then spawns. Measured
    from the built app:

        desktop(43661) → bootloader(43666) → python(43670)   ← holds the radio

    Tauri knows only about the bootloader. Killing it leaves the Python process
    orphaned onto `launchd`, still holding the radio and the port. The next
    launch then reports "no drone found" with a drone plainly sitting there.

    Stdin is the fix because it is the one handle the real process inherits:
    when the parent dies its pipe closes, and the read below returns EOF in the
    process that actually matters. The session is ended first — this path can
    run while the drone is in the air — but within `deadline_s`: noticing the
    parent is gone is only half of it; the exit must happen whatever the
    session does (arm_exit_deadline).
    """

    def watch() -> None:
        try:
            while sys.stdin.readline():
                pass
        except Exception:  # noqa: BLE001 - a closed pipe must not raise here
            pass

        _detach_console_logging()
        log.warning("parent process closed — landing and shutting down")
        finished = arm_exit_deadline(deadline_s)
        try:
            agent.session.end("the app closed")
        except Exception:
            log.exception("could not end the session; stopping the motors")
        finished.set()
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="parent-watchdog").start()


__all__ = ["app", "serve", "agent", "exit_when_parent_closes", "arm_exit_deadline"]
