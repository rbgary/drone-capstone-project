"""Prove a frozen sidecar actually works, before it ships inside an installer.

Bundling failures in this binary are silent. PyInstaller reports success, the
executable starts, and then the one thing that matters — reaching a USB radio,
or upgrading a WebSocket — fails on a user's laptop with a traceback they
cannot act on. Every check here exists because its absence is invisible until
hardware or a user is in the room:

  runs            the bootloader and the Python runtime are intact
  imports         every library the agent loads only on demand loads here
                  (`selftest`) — an Intel build once started and served while
                  the Supabase client could not be imported at all
  usb             libusb loaded and enumerated the bus; cflib's native
                  dependency is the one PyInstaller cannot see by itself
  health          uvicorn serves HTTP
  websocket       uvicorn's websockets implementation is bundled; it is
                  selected by string name, so nothing imports it statically
  exits           closing stdin stops the agent and frees its port — the only
                  signal that reaches the real Python process (see below)

The `usb` check is the subtle one. With no dongle plugged in, a *correct*
binary reports "no drone found" and a *broken* one reports a missing libusb
backend. Both are failures of the command and both exit non-zero, so the exit
code proves nothing — this asserts on which message came back.

The `exits` check is the one that strands a drone. A one-file binary is two
processes, and killing the first leaves the second holding the radio
(rest.py, exit_when_parent_closes). It is proven here on every OS CI builds,
not only on the Mac it was first measured on.

Slow machines are expected, not failures: a one-file binary unpacks ~40 MB
before Python starts, and an older Intel Mac took longer than the 20 s this
used to allow. The limits are generous, and the wait ends early — with the
agent's own output — the moment the process dies, so a crash is never
reported as a timeout.

Run through `build_sidecar.py`, which runs it on every build, or directly:

    python verify_sidecar.py ../../desktop/src-tauri/bin/dronedeck-agent-<triple>
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# A port unlikely to collide with an agent the developer is already running;
# verifying against a *different* process would pass while the frozen binary
# is broken.
PROBE_PORT = 8791
# Measured: ~3 s on an M-series Mac. An older Intel Mac overran 20 s. The limit
# is a ceiling for a hang, not an estimate, so it is far above both.
STARTUP_TIMEOUT_S = 180.0
# `check` starts the runtime AND scans USB.
CHECK_TIMEOUT_S = 240.0
# From closing stdin to the port being free. The agent ends its session first.
EXIT_TIMEOUT_S = 30.0

# What cflib prints once libusb is loaded and it has walked the bus.
USB_REACHED = "Looking for devices"
# The correct answers when no drone can be reached. `check` goes through
# DroneLink, whose reasons come from dronedeck/flight/radio.py; "no drone
# found" is the older core.connect wording. Matching only that one made every
# radio-less build — every CI run — report "a radio answered".
NO_DRONE = (
    "no drone found",
    "No Crazyradio found",           # radio.NOT_FOUND
    "No drone answering",            # radio.NOT_ANSWERING
    "Crazyradio found, but",         # radio.NO_DRIVER / WRONG_DRIVER (Windows)
    "could not be opened",           # radio.unopenable_reason()
)
# What a missing native dependency looks like instead.
BUNDLING_FAILURE = ("No backend available", "ModuleNotFoundError", "ImportError")
# How much of the agent's output to show when it fails.
OUTPUT_TAIL = 4000


class VerificationError(RuntimeError):
    """The binary built, but does not work. Never ship past this."""


def _probe_env(data_dir: str) -> dict[str, str]:
    """The environment the probe agent runs in: isolated from this machine.

    Without this, verifying on a developer's laptop would restore THEIR saved
    sign-in from the real data folder and hold the drone on standby — a build
    step touching the radio.
    """
    env = dict(os.environ)
    env["DRONEDECK_DATA_DIR"] = data_dir
    env["DRONEDECK_STANDBY"] = "0"
    return env


def _check_runs(binary: Path, env: dict[str, str]) -> str:
    started = time.monotonic()
    try:
        result = subprocess.run(
            [str(binary), "--help"], capture_output=True, text=True,
            timeout=STARTUP_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        raise VerificationError(
            f"`--help` did not finish within {STARTUP_TIMEOUT_S:.0f}s"
        ) from None
    if result.returncode != 0:
        raise VerificationError(f"`--help` exited {result.returncode}:\n{result.stderr}")
    return f"the frozen runtime starts ({time.monotonic() - started:.1f}s)"


def _check_imports(binary: Path, env: dict[str, str]) -> str:
    """`selftest` imports what the agent only loads when a feature is used."""
    try:
        result = subprocess.run(
            [str(binary), "selftest"], capture_output=True, text=True,
            timeout=STARTUP_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        raise VerificationError(
            f"`selftest` did not finish within {STARTUP_TIMEOUT_S:.0f}s"
        ) from None
    if result.returncode != 0:
        raise VerificationError(
            "a library the agent needs will not load inside the frozen binary:\n"
            f"{result.stdout}{result.stderr}"
        )
    return (result.stdout.strip().splitlines() or ["imports ok"])[-1].strip()


def _check_usb(binary: Path, env: dict[str, str]) -> str:
    """`check` is expected to fail here — this asserts on *how* it fails."""
    try:
        result = subprocess.run(
            [str(binary), "check"], capture_output=True, text=True,
            timeout=CHECK_TIMEOUT_S, env=env,
        )
    except subprocess.TimeoutExpired:
        raise VerificationError(f"`check` did not finish within {CHECK_TIMEOUT_S:.0f}s") from None
    output = result.stdout + result.stderr

    for marker in BUNDLING_FAILURE:
        if marker in output:
            raise VerificationError(
                f"libusb did not load — {marker!r} in output. The native library "
                f"from libusb_package was not bundled:\n{output}"
            )

    if USB_REACHED not in output:
        raise VerificationError(
            "cflib never enumerated USB, so the radio path is unproven.\n"
            f"Expected {USB_REACHED!r} in:\n{output}"
        )

    for marker in NO_DRONE:
        if marker in output:
            return f"libusb enumerated the bus; no drone reachable (expected: {marker!r})"
    return "libusb enumerated the bus; a drone answered"


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _tail(log_path: Path) -> str:
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return "(no output captured)"
    return text[-OUTPUT_TAIL:] if text.strip() else "(no output)"


def _wait_for_port(process: subprocess.Popen, port: int, log_path: Path) -> float:
    """Seconds until the agent answered. Ends early if the agent dies."""
    started = time.monotonic()
    deadline = started + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise VerificationError(
                f"the agent exited with code {code} before opening port {port}. "
                f"Its output:\n{_tail(log_path)}"
            )
        if _port_open(port):
            return time.monotonic() - started
        time.sleep(0.25)
    raise VerificationError(
        f"the agent never opened port {port} within {STARTUP_TIMEOUT_S:.0f}s and is "
        f"still running. Its output:\n{_tail(log_path)}"
    )


def _kill_tree(process: subprocess.Popen) -> None:
    """Stop the bootloader AND the Python process it started.

    Terminating the bootloader alone is exactly the orphan this file warns
    about, so this is only a clean-up after a failure — never the pass path.
    """
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)


def _stop_like_the_app(process: subprocess.Popen, port: int) -> str:
    """Close stdin, as the desktop app's exit does, and require a clean stop."""
    assert process.stdin is not None
    started = time.monotonic()
    process.stdin.close()
    try:
        process.wait(timeout=EXIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise VerificationError(
            f"closing stdin did not stop the agent within {EXIT_TIMEOUT_S:.0f}s. The "
            f"desktop app would leave it running, holding the radio and the port."
        ) from None
    # The bootloader exits when its child does, so its exit is the child's.
    # The port is the proof: nothing may still be listening.
    while time.monotonic() - started < EXIT_TIMEOUT_S:
        if not _port_open(port):
            return f"closing stdin stops it and frees the port ({time.monotonic() - started:.1f}s)"
        time.sleep(0.25)
    raise VerificationError(
        f"the agent's launcher exited but port {port} is still held — its Python "
        f"process survived. The desktop app would strand it holding the radio."
    )


def _check_server(binary: Path, env: dict[str, str], work: Path) -> list[str]:
    """Start the frozen agent, exercise both surfaces, then stop it as the app does."""
    if _port_open(PROBE_PORT):
        raise VerificationError(
            f"port {PROBE_PORT} is already in use, so this check would talk to "
            f"whatever holds it rather than the new binary. Usually an agent left "
            f"over from an earlier run: find it with "
            + (f"`netstat -ano | findstr :{PROBE_PORT}`" if sys.platform == "win32"
               else f"`lsof -nP -iTCP:{PROBE_PORT} -sTCP:LISTEN`")
            + " and stop it."
        )

    log_path = work / "serve.log"
    with open(log_path, "w") as log:
        process = subprocess.Popen(
            [str(binary), "serve", "--port", str(PROBE_PORT), "--exit-with-parent"],
            stdin=subprocess.PIPE,
            # A file, not a pipe: a pipe nobody reads fills at 64 KB and stalls
            # the very process being timed.
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            # Its own process group, so a failure can kill the whole tree.
            start_new_session=sys.platform != "win32",
        )
    try:
        answered_after = _wait_for_port(process, PROBE_PORT, log_path)
        results = [f"the agent answered after {answered_after:.1f}s"]

        connection = http.client.HTTPConnection("127.0.0.1", PROBE_PORT, timeout=10)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            body = response.read().decode()
            if response.status != 200:
                raise VerificationError(f"/health returned {response.status}: {body}")
            if '"ok":true' not in body.replace(" ", ""):
                raise VerificationError(f"/health did not report ok: {body}")
            results.append("HTTP serves /health")
        finally:
            connection.close()

        # A raw upgrade handshake: the point is uvicorn's websockets
        # implementation answering 101, not what the socket does afterwards.
        #
        # RFC 6455 requires the key to be exactly 16 bytes before encoding.
        # A 17-byte one is rejected with a 400 that looks identical to "the
        # websockets implementation is missing" — which cost a build to tell
        # apart, hence the explicit length. A short one fails the same way: the
        # rename's b"dronedeck1probe" was 15 bytes and failed every build.
        probe = b"dronedeck-1probe"
        assert len(probe) == 16, "RFC 6455: the key is exactly 16 bytes"
        key = base64.b64encode(probe).decode()
        connection = http.client.HTTPConnection("127.0.0.1", PROBE_PORT, timeout=10)
        try:
            connection.request(
                "GET",
                "/ws/live",
                headers={
                    "Connection": "Upgrade",
                    "Upgrade": "websocket",
                    "Sec-WebSocket-Version": "13",
                    "Sec-WebSocket-Key": key,
                },
            )
            response = connection.getresponse()
            if response.status != 101:
                raise VerificationError(
                    f"WebSocket upgrade returned {response.status}, not 101. Either "
                    f"uvicorn's websockets implementation is not bundled, or the live "
                    f"socket's path changed and this check is probing the old one."
                )
            results.append("WebSocket upgrades to 101")
        finally:
            connection.close()

        results.append(_stop_like_the_app(process, PROBE_PORT))
        return results
    finally:
        _kill_tree(process)


def verify(binary: Path) -> None:
    # Absolute: `Path("./agent")` prints as "agent", which the OS then looks
    # for on PATH instead of here.
    binary = binary.resolve()
    if not binary.exists():
        raise VerificationError(f"no such binary: {binary}")

    with tempfile.TemporaryDirectory(prefix="dronedeck-verify-",
                                     ignore_cleanup_errors=True) as work:
        env = _probe_env(str(Path(work) / "data"))
        checks = [
            _check_runs(binary, env),
            _check_imports(binary, env),
            _check_usb(binary, env),
            *_check_server(binary, env, Path(work)),
        ]
    size_mb = binary.stat().st_size / 1_048_576
    for line in checks:
        print(f"  ok   {line}")
    print(f"  ok   single file, {size_mb:.0f} MB")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 2

    try:
        verify(Path(args[0]))
    except VerificationError as e:
        print(f"\n  FAILED  {e}\n", file=sys.stderr)
        return 1

    print("\n  sidecar verified\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
