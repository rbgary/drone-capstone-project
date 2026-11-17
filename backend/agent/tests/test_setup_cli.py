"""`dronedeck setup` — the Set up engine from a terminal, against the fake drone."""

from __future__ import annotations

import pytest

from dronedeck import setup_cli
from dronedeck.cli import build_parser
from tests.test_drone_setup import DONE, FRESH, Rig


@pytest.fixture
def rig(monkeypatch):
    """The command builds its own DroneSetup; hand it the Rig's fake drone."""
    def make(facts):
        r = Rig(facts)
        real = r.setup

        def factory(**kw):
            real._publish = kw["publish"]
            return real
        monkeypatch.setattr(setup_cli, "DroneSetup", factory)
        return r
    monkeypatch.setattr(setup_cli, "agent_running", lambda url=setup_cli.AGENT_HEALTH: False)
    return make


def run(*argv: str) -> int:
    args = build_parser().parse_args(["setup", *argv])
    return setup_cli.cmd_setup(args)


class TestCommand:
    def test_a_new_drone_gets_both_parts_and_no_unplug(self, rig, capsys):
        r = rig(FRESH)
        assert run() == 0
        assert r.flashed == ["main", "lighthouse"]
        out = capsys.readouterr().out
        assert "UNPLUG" not in out                      # that was the AI deck's
        assert "Installing drone firmware" in out

    def test_check_only_writes_nothing(self, rig, capsys):
        r = rig(FRESH)
        assert run("--check") == 0
        assert r.flashed == []
        assert "needed" in capsys.readouterr().out

    def test_a_set_up_drone_is_left_alone(self, rig, capsys):
        r = rig(DONE)
        assert run() == 0
        assert r.flashed == []
        assert "Everything is installed" in capsys.readouterr().out

    def test_reinstall_writes_the_parts_asked_for(self, rig):
        r = rig(DONE)
        assert run("--reinstall", "lighthouse") == 0
        assert r.flashed == ["lighthouse"]

    def test_the_ai_decks_old_parts_are_refused_with_the_reason(self, rig, capsys):
        r = rig(DONE)
        assert run("--reinstall", "camera") == 1
        assert r.flashed == []
        assert "no camera" in capsys.readouterr().out

    def test_an_unknown_part_is_refused_by_name(self, rig, capsys):
        r = rig(DONE)
        assert run("--reinstall", "propellers") == 1
        assert r.flashed == []
        assert "propellers" in capsys.readouterr().out

    def test_no_drone_fails_and_says_so(self, rig):
        rig(None)
        assert run() == 1

    def test_it_refuses_while_droneDeck_holds_the_radio(self, monkeypatch, capsys):
        monkeypatch.setattr(setup_cli, "agent_running", lambda url=setup_cli.AGENT_HEALTH: True)
        monkeypatch.setattr(setup_cli, "DroneSetup",
                            lambda **kw: pytest.fail("must not touch the radio"))
        assert run() == 2
        assert "Quit DroneDeck" in capsys.readouterr().out


class TestCommandLine:
    def test_the_installed_app_runs_its_own_agent(self):
        line = setup_cli.command_line(
            platform="darwin", frozen=True,
            executable="/Applications/DroneDeck.app/Contents/MacOS/dronedeck-agent")
        assert line == "/Applications/DroneDeck.app/Contents/MacOS/dronedeck-agent setup"

    def test_a_path_with_spaces_is_quoted_for_the_shell(self):
        line = setup_cli.command_line(platform="linux", frozen=True,
                                      executable="/opt/Drone Deck/dronedeck-agent")
        assert line == "'/opt/Drone Deck/dronedeck-agent' setup"

    def test_windows_gets_the_powershell_form(self):
        line = setup_cli.command_line(
            platform="win32", frozen=True,
            executable=r"C:\Program Files\DroneDeck\dronedeck-agent.exe")
        assert line == r'& "C:\Program Files\DroneDeck\dronedeck-agent.exe" setup'

    def test_a_developer_runs_the_venv_script(self):
        line = setup_cli.command_line(platform="darwin", frozen=False,
                                      argv0="/repo/backend/agent/.venv/bin/dronedeck")
        assert line == "/repo/backend/agent/.venv/bin/dronedeck setup"

    def test_an_agent_started_another_way_falls_back_to_the_module(self):
        line = setup_cli.command_line(platform="darwin", frozen=False,
                                      executable="/usr/bin/python3", argv0="uvicorn")
        assert line == "/usr/bin/python3 -m dronedeck.cli setup"


def test_nothing_answers_means_droneDeck_is_closed():
    assert setup_cli.agent_running("http://127.0.0.1:9/health") is False
