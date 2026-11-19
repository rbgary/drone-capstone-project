"""The sidecar build and its verification (packaging/).

These are what stand between a broken frozen agent and an installer, so their
failure paths are tested, not just the pass: a crash must be reported as a
crash (not a timeout), and an agent that survives its window must fail the
build, because in the app it would keep the radio.
"""

from __future__ import annotations

import socket
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packaging"))

import build_sidecar  # noqa: E402
import verify_sidecar  # noqa: E402

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake agents are sh scripts")


class TestCpuMatch:
    @pytest.mark.parametrize(("machine", "triple"), [
        ("arm64", "aarch64-apple-darwin"),
        ("x86_64", "x86_64-apple-darwin"),
        ("AMD64", "x86_64-pc-windows-msvc"),
        ("ARM64", "aarch64-pc-windows-msvc"),
    ])
    def test_matching_cpus_pass(self, monkeypatch, machine, triple):
        monkeypatch.setattr(build_sidecar.platform, "machine", lambda: machine)
        build_sidecar.check_cpu(triple)

    def test_an_intel_python_on_an_arm_mac_is_refused(self, monkeypatch):
        """Rosetta would freeze an Intel agent and name it for arm64."""
        monkeypatch.setattr(build_sidecar.platform, "machine", lambda: "x86_64")
        with pytest.raises(build_sidecar.BuildError, match="different processors"):
            build_sidecar.check_cpu("aarch64-apple-darwin")


class TestNoDroneWording:
    def test_every_reason_the_agent_gives_is_recognised(self):
        """`check` reports these when no drone is reachable. If the verifier
        missed one, a radio-less build would claim "a drone answered" — which
        every CI run did until 2026-09-25."""
        from dronedeck.flight import radio

        reasons = [radio.NOT_FOUND, radio.NOT_ANSWERING, radio.NO_DRIVER,
                   radio.WRONG_DRIVER, radio.NO_PERMISSION,
                   "The Crazyradio could not be opened."]
        for reason in reasons:
            assert any(marker in reason for marker in verify_sidecar.NO_DRONE), reason


class TestProbeIsolation:
    def test_the_probe_never_touches_the_operators_data_or_radio(self):
        env = verify_sidecar._probe_env("/tmp/probe-data")
        assert env["DRONEDECK_DATA_DIR"] == "/tmp/probe-data"
        assert env["DRONEDECK_STANDBY"] == "0"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_agent(tmp_path: Path, serve: str) -> Path:
    """An executable that passes --help and check, and does `serve` as told."""
    path = tmp_path / "fake-agent"
    path.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        case "$1" in
          --help) exit 0;;
          selftest) echo "  imports ok (12 checked)"; exit 0;;
          check) echo "Looking for devices"; echo "no drone found"; exit 1;;
          serve) {serve};;
        esac
        """))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@posix_only
class TestVerifyFailures:
    @pytest.fixture(autouse=True)
    def _port(self, monkeypatch):
        self.port = _free_port()
        monkeypatch.setattr(verify_sidecar, "PROBE_PORT", self.port)

    def test_a_crash_is_reported_as_a_crash_at_once(self, tmp_path):
        agent = _fake_agent(tmp_path, 'echo "ModuleNotFoundError: boom"; exit 3')
        started = time.monotonic()
        with pytest.raises(verify_sidecar.VerificationError) as error:
            verify_sidecar.verify(agent)
        assert time.monotonic() - started < 15      # not the 180 s limit
        assert "exited with code 3" in str(error.value)
        assert "ModuleNotFoundError: boom" in str(error.value)

    def test_a_busy_probe_port_is_refused_before_starting(self, tmp_path):
        agent = _fake_agent(tmp_path, "exit 0")
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", self.port))
            holder.listen()
            with pytest.raises(verify_sidecar.VerificationError, match="already in use"):
                verify_sidecar.verify(agent)

    def test_an_agent_that_outlives_its_parent_fails_and_is_killed(self, tmp_path, monkeypatch):
        """The failure that strands a drone: stdin closes, the agent stays."""
        monkeypatch.setattr(verify_sidecar, "EXIT_TIMEOUT_S", 2.0)
        server = tmp_path / "server.py"
        server.write_text(textwrap.dedent(f"""\
            import http.server
            class H(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == "/health":
                        self.send_response(200); self.end_headers()
                        self.wfile.write(b'{{"ok": true}}')
                    else:
                        self.send_response(101); self.send_header("Upgrade", "websocket")
                        self.send_header("Connection", "Upgrade"); self.end_headers()
                def log_message(self, *args): pass
            http.server.HTTPServer(("127.0.0.1", {self.port}), H).serve_forever()
            """))
        agent = _fake_agent(tmp_path, f'exec "{sys.executable}" "{server}"')
        with pytest.raises(verify_sidecar.VerificationError, match="did not stop the agent"):
            verify_sidecar.verify(agent)
        # And nothing is left holding the port afterwards.
        deadline = time.monotonic() + 5
        while verify_sidecar._port_open(self.port) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not verify_sidecar._port_open(self.port)


@posix_only
class TestImportsCheck:
    def test_a_library_that_will_not_load_fails_the_build(self, tmp_path, monkeypatch):
        """What an Intel build hid: it started and served, and could not import
        the Supabase client. Now the build says so."""
        monkeypatch.setattr(verify_sidecar, "PROBE_PORT", _free_port())
        agent = tmp_path / "fake-agent"
        agent.write_text(textwrap.dedent("""\
            #!/bin/sh
            case "$1" in
              --help) exit 0;;
              selftest) echo "  FAILED  supabase: Symbol not found: _SSL_get0_group_name"
                        exit 1;;
            esac
            """))
        agent.chmod(agent.stat().st_mode | stat.S_IXUSR)
        with pytest.raises(verify_sidecar.VerificationError, match="_SSL_get0_group_name"):
            verify_sidecar.verify(agent)


class TestSelftest:
    def test_every_lazily_loaded_library_loads_here(self, capsys):
        from dronedeck.cli import cmd_selftest

        assert cmd_selftest(None) == 0                     # type: ignore[arg-type]
        assert "imports ok" in capsys.readouterr().out

    def test_a_broken_one_is_named(self, monkeypatch, capsys):
        from dronedeck import cli

        monkeypatch.setattr(cli, "SELFTEST_MODULES", ("no_such_module_anywhere",))
        assert cli.cmd_selftest(None) == 1                 # type: ignore[arg-type]
        assert "no_such_module_anywhere" in capsys.readouterr().out
