"""`dronedeck setup` — put the drone's software on it from a terminal.

The Set up page's engine (drone_setup.DroneSetup), run as a command: the same
check and the same parts in the same order. The Set up page shows this command to copy rather
than an Install button (2026-10-24, Samuel: "put it in a command — which the
user can run and get the firmware on the drone"), so an install is something
you watch run, with every step printed, not a button whose progress hides in
a page.

THE RADIO HAS ONE OWNER. While DroneDeck is open its agent holds the
Crazyradio, and a second program cannot reach the drone — it would report "no
drone" and mislead. So this checks first whether DroneDeck's agent answers
and, if it does, says to quit it.

    dronedeck setup                          check, then install what is needed
    dronedeck setup --check                  check only; change nothing
    dronedeck setup --reinstall lighthouse   write that part again too

Installed app, no Python: the app's own agent binary is the whole CLI
(packaging/sidecar.py) — `…/DroneDeck.app/Contents/MacOS/dronedeck-agent
setup`. GET /setup/command gives this machine's exact line (command_line).
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from dronedeck.drone_setup import PART_LABEL, PART_ORDER, DroneSetup, SetupError

#: Where DroneDeck's agent listens (api/rest.py, cli serve's default).
AGENT_HEALTH = "http://127.0.0.1:8765/health"
#: Longest an install may take: main and the positioning deck. (It was 40 min
#: while the AI deck's battery unplug — up to 5 min alone — was part of it.)
INSTALL_TIMEOUT_S = 20 * 60


def agent_running(url: str = AGENT_HEALTH) -> bool:
    """DroneDeck's agent answers — it holds the radio."""
    try:
        with urllib.request.urlopen(url, timeout=1.0) as reply:     # noqa: S310 — localhost
            return bool(json.load(reply).get("ok"))
    except (OSError, ValueError):
        return False


def command_line(platform: str = sys.platform, frozen: bool | None = None,
                 executable: str | None = None, argv0: str | None = None) -> str:
    """The command that runs `setup` on THIS machine, ready to paste.

    An installed app has no Python on PATH: its own agent binary is the CLI.
    A developer's agent runs from the venv's `dronedeck` script."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    program = (executable or sys.executable) if frozen else (argv0 or sys.argv[0])
    if not frozen and not Path(program).name.startswith("dronedeck"):
        return f"{_quote(executable or sys.executable, platform)} -m dronedeck.cli setup"
    if platform.startswith("win"):
        # PowerShell, Windows' default terminal: & runs a quoted path.
        return f'& "{program}" setup'
    return f"{_quote(program, platform)} setup"


def _quote(path: str, platform: str) -> str:
    return f'"{path}"' if platform.startswith("win") else shlex.quote(path)


def cmd_setup(args: argparse.Namespace) -> int:
    if agent_running():
        print("\n  DroneDeck is open, and it holds the radio — this cannot reach the drone"
              "\n  while it does. Quit DroneDeck (DroneDeck › Quit, or Cmd-Q), then run"
              "\n  this again.\n")
        return 2
    if not args.verbose:
        # The steps below are the output; cflib's link chatter still goes to
        # the log file (cli._start_logging), and -v brings it back here.
        for handler in logging.getLogger().handlers:
            if type(handler) is logging.StreamHandler:
                handler.setLevel(logging.WARNING)
    reinstall =[p.strip() for p in (args.reinstall or "").split(",") if p.strip()]
    printer = _Printer()
    setup = DroneSetup(pause_radio=lambda: None, resume_radio=lambda: None,
                       publish=printer.publish)

    print("\n  Looking for the drone…")
    setup.check()
    setup.wait(120)
    state = setup.state()
    printer.parts(state)
    if state["phase"] == "failed":
        print(f"\n  {state['message']}\n")
        return 1
    if args.check:
        print("\n  Checked only — nothing written. Run without --check to install.\n")
        return 0
    if state["phase"] == "done" and not reinstall:
        print("\n  Everything is installed. Use --reinstall <parts> to write any again.\n")
        return 0

    try:
        setup.install(reinstall)
    except SetupError as e:
        print(f"\n  {e}\n")
        return 1
    deadline = time.monotonic() + INSTALL_TIMEOUT_S
    while setup.busy() and time.monotonic() < deadline:
        setup.wait(1.0)
    state = setup.state()
    if setup.busy():
        print("\n  Still running after 20 minutes — something is stuck. Ctrl-C, then run "
              "this again: it continues from where it stopped.\n")
        return 1
    printer.parts(state)
    print(f"\n  {state['message']}\n")
    return 0 if state["phase"] == "done" else 1


class _Printer:
    """Each step once, progress in fifths."""

    def __init__(self) -> None:
        self._phase: str | None = None
        self._current: str | None = None
        self._fifth = -1

    def publish(self, state: dict[str, Any]) -> None:
        phase, current = state.get("phase"), state.get("current")
        if phase == "installing" and current != self._current and current:
            print(f"\n  Installing {PART_LABEL.get(current, current).lower()}…", flush=True)
            self._fifth = -1
        elif phase == "installing":
            fifth = int((state.get("progress") or 0.0) * 5)
            if fifth != self._fifth:
                self._fifth = fifth
                print(f"    {'█' * fifth}{'·' * (5 - fifth)}  {fifth * 20}%", flush=True)
        self._phase, self._current = phase, current

    @staticmethod
    def parts(state: dict[str, Any]) -> None:
        parts = state.get("parts") or {}
        facts = state.get("facts") or {}
        if facts.get("battery_v") is not None:
            print(f"\n  Battery {facts['battery_v']:.2f} V")
        if not parts:
            return
        print()
        for part in PART_ORDER:
            if part in parts:
                print(f"    {PART_LABEL[part]:<22} {parts[part]}")


def add_parser(sub: Any) -> None:
    setup = sub.add_parser(
        "setup", help="install the drone's software: firmware and positioning deck")
    setup.add_argument("--check", action="store_true",
                       help="only read what is installed; change nothing")
    setup.add_argument("--reinstall", metavar="PARTS",
                       help="also write these again, comma-separated: "
                            + ", ".join(PART_ORDER))
