"""Set up a drone's software: the desktop's Set up page, step 2.

A new Crazyflie 2.1 runs stock firmware. Two pieces ship inside the agent
(firmware_bundle/, with a checksummed manifest):

    main        the drone's STM32: stock 2025.12.1 + drone_wifi v4
    lighthouse  the Lighthouse deck's FPGA: Bitcraze's lighthouse.bin from the
                SAME 2025.12.1 release (lighthouse-fpga V6)

NO AI DECK (2026-11-15). The drone carries no camera (docs/features/
architecture.txt, DECISIONS), so the AI deck's two chips — the GAP8 streamer
and the patched ESP32 — are no longer installed, and a drone without the deck
is set up like any other. (Until then a drone with no AI deck was REFUSED,
before main or the positioning deck were written.)

WHY MAIN IS STILL drone_wifi (Option A, 2026-11-15). drone_wifi's Wi-Fi relay
is dead code now — its loop waits on the app channel and nothing sends — but
it also holds cwsetup.fw, the one marker that says main is ours and current.
Keeping the exact image already flown (flight-log.txt: v4) means no drone is
reflashed for this change and a new drone gets a proven build. The cleanup
that drops the custom build (Option C: Bitcraze's own cf2.bin, main detected
by firmware.revision0) is written up in docs/features/desktop/drone-wifi.txt.

THE LIGHTHOUSE DECK (added 2026-10-01). A new deck can arrive without the image
its drone's firmware asks for: it is detected, sits in its bootloader, and
receives nothing — every position reads as lost, and the base stations look
broken when they are not. The drone SAYS so (its deck memory reports
fw_upgrade_required), so that is read rather than assumed, and read again
after main is flashed, because main decides which image the deck needs.

WHAT IS ALREADY INSTALLED is read from the drone, never assumed:
    cwsetup.fw      drone_wifi's version — present only when "main" is ours
    lighthouse      the deck's own fw_upgrade_required (no marker needed)
Only what is missing or older is flashed — plus whatever the operator asks to
reinstall anyway. (cwsetup.deck and cwsetup.deckid, the AI deck's markers,
may still be stored on a drone; nothing reads them.)

ORDER: main first (it reboots the drone), lighthouse straight after (main
names the image it requires).

The agent is the radio's only owner: standby is paused while this runs and
resumed after. Refused during a session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

BUNDLE_DIR = Path(__file__).parent / "firmware_bundle"
URI = "radio://0/80/2M"
MIN_BATTERY_V = 3.8
PART_ORDER = ("main", "lighthouse")
PART_LABEL = {"main": "Drone firmware", "lighthouse": "Positioning deck"}


class SetupError(RuntimeError):
    """A refusal or failure in words the operator can act on."""


@dataclass(frozen=True)
class Facts:
    """What the drone says about itself."""

    battery_v: float | None
    fw_version: int | None       # cwsetup.fw; None = not our firmware
    #: deck.bcLighthouse4: the positioning deck is detected. None = not asked.
    lighthouse_deck: bool | None = None
    #: The drone's own word that the deck needs its firmware (deck memory).
    lighthouse_needs_fw: bool = False


@dataclass
class SetupState:
    #: idle · checking · ready · installing · done · failed
    phase: str = "idle"
    message: str | None = None
    facts: dict[str, Any] | None = None
    #: part -> "installed" | "needed" | "installing" | "done"
    parts: dict[str, str] = field(default_factory=dict)
    current: str | None = None
    progress: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "message": self.message, "facts": self.facts,
                "parts": self.parts, "current": self.current,
                "progress": round(self.progress, 3),
                "labels": PART_LABEL, "order": list(PART_ORDER)}


def load_manifest(bundle: Path = BUNDLE_DIR) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((bundle / "manifest.json").read_text())
    return manifest


def verified_file(bundle: Path, part: str, manifest: dict[str, Any]) -> Path:
    """The part's firmware, refused if it is not byte-for-byte the tested one."""
    entry = manifest["parts"][part]
    path: Path = bundle / str(entry["file"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise SetupError(f"The bundled {PART_LABEL[part].lower()} file is damaged. "
                         "Reinstall DroneDeck.")
    return path


def needed_parts(facts: Facts, manifest: dict[str, Any]) -> dict[str, str]:
    """Which parts to install."""
    main_ok = facts.fw_version is not None and facts.fw_version >= manifest["drone_wifi"]
    return {
        "main": "installed" if main_ok else "needed",
        "lighthouse": lighthouse_part(facts),
    }


def reinstall_set(parts: Iterable[str]) -> frozenset[str]:
    """The parts asked for again, refused by name if unknown. "camera" and
    "wifi" were the AI deck's, removed 2026-11-15: asked for, they say so."""
    chosen = set(parts)
    retired = chosen & {"camera", "wifi"}
    if retired:
        raise SetupError(f"{' and '.join(sorted(retired))} were the AI deck's parts; the "
                         "drone carries no camera now, so there is nothing to reinstall.")
    unknown = chosen - set(PART_ORDER)
    if unknown:
        raise SetupError(f"There is no part called {', '.join(sorted(unknown))} to reinstall.")
    return frozenset(chosen)


def lighthouse_part(facts: Facts) -> str:
    """The positioning deck reports its own state — no marker is needed."""
    if facts.lighthouse_needs_fw:
        return "needed"
    return "absent" if facts.lighthouse_deck is False else "installed"


# ── talking to the drone (replaced in tests) ─────────────────────────────


def _with_drone(fn: Callable[[Any], Any], timeout_s: float = 30.0) -> Any:
    """Run fn(cf) on a fresh link, or None when no drone answers in time."""
    import cflib.crtp
    from cflib.crazyflie import Crazyflie
    from cflib.crazyflie.syncCrazyflie import SyncCrazyflie

    from dronedeck.paths import cflib_cache_dir

    cflib.crtp.init_drivers()
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            with SyncCrazyflie(URI, cf=Crazyflie(rw_cache=str(cflib_cache_dir()))) as scf:
                out["value"] = fn(scf.cf)
        except Exception as e:                              # noqa: BLE001
            out["error"] = e

    worker = threading.Thread(target=run, daemon=True, name="setup-link")
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive() or "value" not in out:
        return None
    return out["value"]


def _read_facts(cf: Any) -> Facts:
    from cflib.crazyflie.log import LogConfig

    def param(name: str) -> str | None:
        group, _, item = name.partition(".")
        if group not in cf.param.toc.toc or item not in cf.param.toc.toc[group]:
            return None
        return str(cf.param.get_value(name))

    got: dict[str, float] = {}
    conf = LogConfig("setup", 100)
    conf.add_variable("pm.vbat", "float")
    conf.data_received_cb.add_callback(lambda _t, data, _c: got.update(data))
    cf.log.add_config(conf)
    conf.start()
    time.sleep(0.6)
    conf.stop()
    fw = param("cwsetup.fw")
    lighthouse = param("deck.bcLighthouse4")
    return Facts(
        battery_v=round(got["pm.vbat"], 2) if "pm.vbat" in got else None,
        fw_version=None if fw is None else int(fw),
        lighthouse_deck=None if lighthouse is None else lighthouse == "1",
        lighthouse_needs_fw=lighthouse == "1" and _deck_needs_fw(cf, "bcLighthouse4"),
    )


def _deck_needs_fw(cf: Any, name: str) -> bool:
    """The drone's deck memory: does this deck need its firmware written?"""
    from cflib.crazyflie.mem import MemoryElement
    from cflib.crazyflie.mem.deck_memory import SyncDeckMemoryManager

    mems = cf.mem.get_mems(MemoryElement.TYPE_DECK_MEMORY)
    if not mems:
        return False
    for deck in SyncDeckMemoryManager(mems[0]).query_decks().values():
        if deck.name == name:
            return bool(deck.is_fw_upgrade_required)
    return False


def probe_drone() -> Facts | None:
    facts = _with_drone(_read_facts)
    return facts if isinstance(facts, Facts) else None


def flash_part(part: str, path: Path, on_progress: Callable[[float], None]) -> bool:
    if part == "main":
        from dronedeck.flash_firmware import flash as flash_main
        return flash_main(path, URI, on_progress) == 0
    from dronedeck.flash_firmware import flash_lighthouse_deck
    return flash_lighthouse_deck(path, URI, on_progress) == 0


# ── the procedure ────────────────────────────────────────────────────────


class DroneSetup:
    """The Set up page's engine. Every step runs on one worker thread and every
    change is published, so the page shows exactly where it is."""

    def __init__(
        self,
        *,
        pause_radio: Callable[[], None],
        resume_radio: Callable[[], None],
        publish: Callable[[dict[str, Any]], None] = lambda _s: None,
        bundle: Path = BUNDLE_DIR,
        probe: Callable[[], Facts | None] = probe_drone,
        flash: Callable[[str, Path, Callable[[float], None]], bool] = flash_part,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._pause, self._resume, self._publish = pause_radio, resume_radio, publish
        self._bundle = bundle
        self._probe, self._flash = probe, flash
        self._sleep = sleep
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._state = SetupState()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state.to_dict()

    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def check(self) -> None:
        self._start(self._do_check)

    def install(self, reinstall: Iterable[str] = ()) -> None:
        """Install what is missing, plus the parts in `reinstall` even though
        the drone reports them installed."""
        again = reinstall_set(reinstall)
        self._start(lambda: self._do_install(again))

    def wait(self, timeout: float = 30.0) -> None:
        if self._worker is not None:
            self._worker.join(timeout)

    # ── steps ────────────────────────────────────────────────────────────

    def _start(self, work: Callable[[], None]) -> None:
        if self.busy():
            raise SetupError("Set up is already running.")

        def run() -> None:
            self._pause()
            try:
                work()
            except SetupError as e:
                self._set(phase="failed", message=str(e), current=None)
            except Exception:
                log.exception("drone setup failed")
                self._set(phase="failed", current=None,
                          message="Set up stopped unexpectedly. Press the button again "
                                  "to continue where it stopped.")
            finally:
                self._resume()

        self._worker = threading.Thread(target=run, name="drone-setup", daemon=True)
        self._worker.start()

    def _do_check(self) -> None:
        self._set(phase="checking", message="Looking for the drone…", parts={}, progress=0.0)
        facts = self._facts()
        manifest = load_manifest(self._bundle)
        parts = needed_parts(facts, manifest)
        if all(v in ("installed", "absent") for v in parts.values()):
            self._set(phase="done", parts=parts,
                      message="This drone's software is already installed.")
        else:
            self._set(phase="ready", parts=parts, message=None)

    def _do_install(self, again: frozenset[str] = frozenset()) -> None:
        self._do_check()
        with self._lock:
            parts = dict(self._state.parts)
        # Asked for again: an installed part is written anyway. A positioning
        # deck that is not fitted stays absent — there is nothing to write it to.
        for part in again:
            if parts.get(part) == "installed":
                parts[part] = "needed"
        if not any(v == "needed" for v in parts.values()):
            return                     # the check already said it is all installed
        self._set(phase="ready", parts=dict(parts), message=None)
        facts = self._facts()
        if facts.battery_v is not None and facts.battery_v < MIN_BATTERY_V:
            raise SetupError(f"The battery is at {facts.battery_v:.2f} V. Charge it above "
                             f"{MIN_BATTERY_V} V first — a flash that loses power can "
                             "leave the drone unable to start.")
        manifest = load_manifest(self._bundle)
        for part in PART_ORDER:
            if part == "lighthouse" and parts["main"] == "done" and part not in again:
                # Main decides which image the deck needs: ask again now.
                parts[part] = lighthouse_part(self._facts())
            if parts[part] != "needed":
                continue
            path = verified_file(self._bundle, part, manifest)
            parts[part] = "installing"
            self._set(phase="installing", parts=dict(parts), current=part, progress=0.0,
                      message=f"Installing {PART_LABEL[part].lower()}. Keep the drone "
                              "switched on and the dongle plugged in.")
            if not self._flash(part, path, lambda f: self._set(progress=f)):
                raise SetupError(f"Installing {PART_LABEL[part].lower()} stopped. Check the "
                                 "drone is on and the dongle is plugged in, then press "
                                 "Install again — it continues from here.")
            parts[part] = "done"
            self._set(parts=dict(parts), progress=1.0)
            self._sleep(6.0)              # the drone reboots into the new firmware
            if part == "lighthouse" and self._facts().lighthouse_needs_fw:
                raise SetupError("The positioning deck still asks for its firmware after "
                                 "it was written. Press Install again.")

        self._set(phase="done", current=None,
                  parts={p: "absent" if parts.get(p) == "absent" else "installed"
                         for p in PART_ORDER},
                  message="The drone's software is installed.")

    def _facts(self) -> Facts:
        facts = self._probe()
        if facts is None:
            raise SetupError("No drone found. Plug in the Crazyradio and switch the drone "
                             "on, then try again.")
        self._set(facts={"battery_v": facts.battery_v})
        return facts

    def _set(self, **changes: Any) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self._state, key, value)
            payload = self._state.to_dict()
        try:
            self._publish(payload)
        except Exception:
            log.exception("setup publish failed")
