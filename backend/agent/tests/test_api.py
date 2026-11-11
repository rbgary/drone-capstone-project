"""The agent's HTTP and WebSocket surface.

Tested with FastAPI's TestClient and no drone. The endpoints that matter here
are the ones that must refuse: an unauthorised caller, a command before the
checks, and a flight before the operator confirms the area.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dronedeck.api import rest
from dronedeck.api.tokens import HEADER
from dronedeck.session import Session, State
from dronedeck.sync.cloud import Operator
from dronedeck.sync.outbox import Outbox
from tests.test_session import FakeLink
from tests.test_sync import FakeCloud

COMMANDS = [
    ("post", "/auth/sign-in", {"email": "a@b.c", "password": "x"}),
    ("post", "/auth/sign-out", None),
    ("post", "/session/start", None),
    ("post", "/session/confirm", None),
    ("post", "/session/prop-test", None),
    ("post", "/session/health-test", None),
    ("post", "/session/retry", None),
    ("post", "/session/reset-drone", None),
    ("post", "/session/program", {"height_m": 0.3, "hold_s": 5}),
    ("post", "/session/manual/arm", {}),
    ("post", "/session/land", None),
    ("post", "/session/emergency-stop", None),
    ("post", "/session/end", None),
    ("post", "/session/mode", {"mode": "manual"}),
    ("post", "/controls/frame", {"frame": "room"}),
    ("post", "/controls/operator", {"x": 0.0, "y": 0.0}),
    ("post", "/sync/now", None),
    ("get", "/session", None),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DRONEDECK_DATA_DIR", str(tmp_path))
    # Standby is exercised in test_session.py, driven step by step; a
    # background loop here would race every API test.
    monkeypatch.setenv("DRONEDECK_STANDBY", "0")
    cloud = FakeCloud()
    cloud.sign_in = lambda email, password: Operator("user-1", email, "Ada", "operator")
    cloud.sign_out = lambda: None
    link = FakeLink()
    session = Session(cloud=cloud, outbox=Outbox(tmp_path / "outbox"),
                      link_factory=lambda: link, publish=rest.agent.hub.publish)
    monkeypatch.setattr(rest.agent, "session", session)
    monkeypatch.setattr(rest.agent, "cloud", cloud)
    test_client = TestClient(rest.app)
    test_client.link = link          # type: ignore[attr-defined]
    return test_client


def auth(client) -> dict[str, str]:
    return {HEADER: rest.agent.token}


class TestOpenEndpoints:
    def test_health_needs_no_token_and_no_drone(self, client):
        response = client.get("/health")
        assert response.status_code == 200 and response.json()["ok"] is True

    def test_status_is_a_summary_without_operator_details(self, client):
        body = client.get("/status").json()
        assert body["signed_in"] is False and body["drone_connected"] is False
        assert "operator" not in body


class TestControlToken:
    @staticmethod
    def call(client, method, path, payload, headers=None):
        if method == "get":
            return client.get(path, headers=headers)
        return client.post(path, json=payload, headers=headers)

    @pytest.mark.parametrize(("method", "path", "payload"), COMMANDS)
    def test_every_command_needs_the_token(self, client, method, path, payload):
        assert self.call(client, method, path, payload).status_code == 401, path

    @pytest.mark.parametrize(("method", "path", "payload"), COMMANDS)
    def test_a_wrong_token_is_refused(self, client, method, path, payload):
        response = self.call(client, method, path, payload, {HEADER: "guessed"})
        assert response.status_code == 401, path

    def test_the_right_token_is_accepted(self, client):
        assert client.get("/session", headers=auth(client)).status_code == 200


class TestSessionFlow:
    def test_sign_in_then_checks_then_confirm_then_fly(self, client, monkeypatch):
        from dronedeck.flight.programs import Outcome, ProgramResult
        from dronedeck.safety.flight_guard import Reason

        monkeypatch.setattr(
            "dronedeck.session.run_hover_test",
            lambda flight, program: ProgramResult(Outcome.COMPLETED, Reason.NONE, "done"),
        )
        headers = auth(client)
        assert client.post("/auth/sign-in", json={"email": "a@b.c", "password": "x"},
                           headers=headers).json()["state"] == "idle"

        client.post("/session/start", headers=headers)
        rest.agent.session.wait_idle()
        assert rest.agent.session.snapshot().state is State.AWAITING_CONFIRMATION

        assert client.post("/session/confirm", headers=headers).json()["state"] == "ready"
        client.post("/session/program", json={"height_m": 0.3, "hold_s": 5}, headers=headers)
        rest.agent.session.wait_idle()
        assert rest.agent.session.snapshot().state is State.READY

    def test_flying_before_confirming_is_refused_with_a_reason(self, client):
        headers = auth(client)
        client.post("/auth/sign-in", json={"email": "a@b.c", "password": "x"}, headers=headers)
        response = client.post("/session/program", json={}, headers=headers)
        assert response.status_code == 409
        assert "confirm the area" in response.json()["detail"]

    def test_flying_without_signing_in_is_refused(self, client):
        response = client.post("/session/start", headers=auth(client))
        assert response.status_code == 409
        assert "Sign in" in response.json()["detail"]

    def test_a_viewer_cannot_sign_in_to_fly(self, client):
        rest.agent.cloud.sign_in = lambda e, p: Operator("u", e, None, "viewer")
        response = client.post("/auth/sign-in", json={"email": "v@b.c", "password": "x"},
                               headers=auth(client))
        assert response.status_code == 409 and "not fly" in response.json()["detail"]

    def test_program_bounds_are_validated_at_the_boundary(self, client):
        headers = auth(client)
        too_high = client.post("/session/program", json={"height_m": 5.0}, headers=headers)
        too_long = client.post("/session/program", json={"hold_s": 600}, headers=headers)
        assert (too_high.status_code, too_long.status_code) == (422, 422)


class TestLiveSocket:
    def test_the_socket_needs_the_token_in_its_first_message(self, client):
        with client.websocket_connect("/ws/live") as ws:
            ws.send_json({"type": "auth", "token": "wrong"})
            assert ws.receive_json()["type"] == "error"

    def test_an_authorised_socket_receives_the_session_state(self, client):
        with client.websocket_connect("/ws/live") as ws:
            ws.send_json({"type": "auth", "token": rest.agent.token})
            first = ws.receive_json()
            assert first["type"] == "session" and first["state"] == "signed_out"

    def test_an_unknown_origin_is_refused_before_anything_else(self, client):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/ws/live", headers={"origin": "https://evil.example"}
        ) as ws:
            ws.receive_json()

    def test_the_desktop_origin_is_accepted(self, client):
        with client.websocket_connect("/ws/live", headers={"origin": "tauri://localhost"}) as ws:
            ws.send_json({"type": "auth", "token": rest.agent.token})
            assert ws.receive_json()["type"] == "session"


class TestCors:
    def test_the_site_may_read_health(self, client):
        response = client.get("/health", headers={"origin": "https://drone-capstone-project.vercel.app"})
        assert response.headers["access-control-allow-origin"] == "https://drone-capstone-project.vercel.app"

    def test_an_unknown_site_gets_no_grant(self, client):
        response = client.get("/health", headers={"origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers

    def test_a_website_may_not_preflight_a_command(self, client):
        response = client.options("/session/start", headers={
            "origin": "https://drone-capstone-project.vercel.app",
            "access-control-request-method": "POST",
        })
        assert response.status_code == 400

    @pytest.mark.parametrize("origin", rest.DESKTOP_ORIGINS)
    def test_the_desktop_window_may_preflight_a_command(self, client, origin):
        """The window is a browser too.

        Allowing GET alone broke every button in the app — WebKit refused the
        preflight, so each command surfaced as "could not reach the agent" while
        the WebSocket, which CORS does not cover, kept working. Nothing caught
        it because the tests only checked that a *website* was refused.
        """
        response = client.options("/session/start", headers={
            "origin": origin,
            "access-control-request-method": "POST",
            "access-control-request-headers": HEADER.lower(),
        })
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin
        assert HEADER.lower() in response.headers["access-control-allow-headers"].lower()

    def test_a_desktop_command_still_needs_the_token(self, client):
        """CORS is not the gate. The token is."""
        response = client.post("/session/start", headers={"origin": rest.DESKTOP_ORIGINS[0]})
        assert response.status_code == 401

    def test_private_network_preflight_is_answered(self, client):
        response = client.options("/health", headers={
            "origin": "https://drone-capstone-project.vercel.app",
            "access-control-request-method": "GET",
            "access-control-request-private-network": "true",
        })
        assert response.headers.get("access-control-allow-private-network") == "true"


class TestUnexpectedErrors:
    """An agent bug must reach the window as words, not as silence.

    Starlette's own 500 carries no CORS headers, so the window's browser threw
    the reply away and showed "The flight agent is running but did not answer
    /auth/sign-in" — for an Intel Mac whose sign-in was failing inside the
    agent the whole time (2026-09-25)."""

    def _sign_in(self, client, monkeypatch, error):
        def fail(email, password):
            raise error
        monkeypatch.setattr(rest.agent.session._cloud, "sign_in", fail)
        return client.post(
            "/auth/sign-in", json={"email": "ada@example.com", "password": "pw"},
            headers={"origin": rest.DESKTOP_ORIGINS[0], HEADER: rest.agent.token},
        )

    def test_the_window_can_read_it(self, client, monkeypatch):
        response = self._sign_in(client, monkeypatch, ImportError("no h2"))
        assert response.status_code == 500
        assert response.headers["access-control-allow-origin"] == rest.DESKTOP_ORIGINS[0]
        detail = response.json()["detail"]
        assert "unexpected error (ImportError)" in detail and "log" in detail

    def test_it_is_written_to_the_log_in_full(self, client, monkeypatch, caplog):
        with caplog.at_level("ERROR", logger=rest.log.name):
            self._sign_in(client, monkeypatch, RuntimeError("boom"))
        [record] = [r for r in caplog.records if "unexpected error" in r.getMessage()]
        assert "POST /auth/sign-in" in record.getMessage()
        assert record.exc_info is not None                  # the traceback, not just a line

    def test_the_message_never_carries_the_raw_error_text(self, client, monkeypatch):
        response = self._sign_in(client, monkeypatch, RuntimeError("password=hunter2"))
        assert "hunter2" not in response.text


class TestCorsRefusalLog:
    """A refused preflight reaches the page as "could not reach the agent" —
    the same words as an agent that is not running. The log tells them apart."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        monkeypatch.setattr(rest, "_cors_refusals_logged", set())

    def _preflight(self, client, origin, method="POST", path="/auth/sign-in"):
        return client.options(path, headers={
            "origin": origin, "access-control-request-method": method,
        })

    def test_a_refused_origin_is_logged_with_what_it_asked_for(self, client, caplog):
        with caplog.at_level("WARNING", logger=rest.log.name):
            assert self._preflight(client, "null").status_code == 400
        [line] = [r.getMessage() for r in caplog.records if "cross-origin" in r.getMessage()]
        assert "'null'" in line and "POST /auth/sign-in" in line and "origin not allowed" in line

    def test_a_refused_method_is_logged(self, client, caplog):
        with caplog.at_level("WARNING", logger=rest.log.name):
            self._preflight(client, "https://drone-capstone-project.vercel.app")
        assert any("method not allowed" in r.getMessage() for r in caplog.records)

    def test_a_retrying_page_is_logged_once(self, client, caplog):
        with caplog.at_level("WARNING", logger=rest.log.name):
            for _ in range(5):
                self._preflight(client, "https://evil.example")
        lines = [r for r in caplog.records if "cross-origin" in r.getMessage()]
        assert len(lines) == 1

    def test_invented_origins_cannot_grow_it_without_bound(self, client):
        for i in range(rest.CORS_REFUSALS_LOGGED_MAX + 20):
            self._preflight(client, f"https://{i}.example")
        assert len(rest._cors_refusals_logged) == rest.CORS_REFUSALS_LOGGED_MAX

    def test_an_allowed_preflight_is_not_logged(self, client, caplog):
        with caplog.at_level("WARNING", logger=rest.log.name):
            self._preflight(client, rest.DESKTOP_ORIGINS[0])
        assert not any("cross-origin" in r.getMessage() for r in caplog.records)


class TestQuietHealthLog:
    def test_health_is_logged_a_few_times_then_not(self):
        import logging

        health_filter = rest.QuietHealthFilter(keep=3)
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                                   '127.0.0.1 - "GET /health HTTP/1.1" 200', None, None)
        assert [health_filter.filter(record) for _ in range(5)] == [True, True, True, False, False]

    def test_other_requests_are_always_logged(self):
        import logging

        health_filter = rest.QuietHealthFilter(keep=1)
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                                   '127.0.0.1 - "POST /session/start HTTP/1.1" 200', None, None)
        assert all(health_filter.filter(record) for _ in range(5))


class TestHistoryApi:
    def test_history_needs_the_token(self, client):
        assert client.get("/history/sessions").status_code == 401

    def test_lists_sessions_and_reads_samples(self, client):
        from dronedeck.history import SessionLog, SessionMeta

        log_ = SessionLog(SessionMeta(
            id="3f1c2a", operator_id="u", operator_email="ada@example.com", operator_name="Ada",
            drone_hardware_id="cf-lab", mode="manual", assisted=False,
            started_at="2026-09-16T20:00:00+00:00",
        ))
        from types import MappingProxyType, SimpleNamespace
        log_.sample(SimpleNamespace(values=MappingProxyType({"pm.vbat": 3.9})))
        log_.close("operator")

        headers = auth(client)
        listed = client.get("/history/sessions", headers=headers).json()
        assert [s["id"] for s in listed] == ["3f1c2a"]
        one = client.get("/history/sessions/3f1c2a", headers=headers).json()
        assert one["operator_email"] == "ada@example.com"
        rows = client.get("/history/sessions/3f1c2a/samples?vars=pm.vbat", headers=headers).json()
        assert rows[0]["pm.vbat"] == 3.9

    def test_an_unknown_session_is_a_404(self, client):
        response = client.get("/history/sessions/nope", headers=auth(client))
        assert response.status_code == 404


class TestRecordingRoutes:
    """A session's frames, read back from disk through the API."""

    def test_nothing_is_recording_outside_a_session(self, client):
        assert client.get("/camera/recording", headers=auth(client)).json() == {"recording": False}
        assert client.get("/camera/recording/frame/latest", headers=auth(client)).status_code == 204

    def test_frames_are_served_from_disk_by_number_and_latest(self, client, tmp_path):
        recording = rest.agent.recorder.start("s-api", tmp_path / "s-api")
        try:
            rest.agent.recorder.on_frame(b"frame-one", "image/png", 2, 1)
            rest.agent.recorder.on_frame(b"frame-two", "image/png", 2, 1)
            summary = client.get("/camera/recording", headers=auth(client)).json()
            assert (summary["recording"], summary["count"], summary["latest_seq"]) == (True, 2, 2)
            latest = client.get("/camera/recording/frame/latest", headers=auth(client))
            assert latest.content == b"frame-two"
            assert latest.headers["cache-control"] == "no-store"
            first = client.get("/camera/recording/frame/1", headers=auth(client))
            assert first.content == b"frame-one"
            listed = client.get("/camera/recording/frames?after=1&limit=5", headers=auth(client))
            assert [f["seq"] for f in listed.json()["frames"]] == [2]
            assert recording.count == 2
        finally:
            rest.agent.recorder.stop()

    def test_recorded_frames_need_the_token(self, client):
        """Unlike the live frame, a recording is session data."""
        for path in ("/camera/recording", "/camera/recording/frame/latest",
                     "/camera/recording/frames"):
            assert client.get(path).status_code == 401

    def test_a_bad_frame_number_is_a_404_not_a_crash(self, client, tmp_path):
        rest.agent.recorder.start("s-api", tmp_path / "s-api")
        try:
            bad = client.get("/camera/recording/frame/abc", headers=auth(client))
            beyond = client.get("/camera/recording/frame/99", headers=auth(client))
            assert (bad.status_code, beyond.status_code) == (404, 204)
        finally:
            rest.agent.recorder.stop()


class TestArrowKeyControls:
    """Which way the arrow keys move the drone (flight/keyframe.py)."""

    def test_the_snapshot_carries_the_defaults(self, client):
        body = client.get("/session", headers=auth(client)).json()
        assert body["controls"] == {"key_frame": "operator", "operator": None,
                                    "operator_marked_at": None, "live": None}

    def test_the_nose_speed_and_correction_endpoints_are_gone(self, client):
        for path, body in (("/controls/nose", {"nose": "left"}),
                           ("/controls/speed", {"speed": "slow"}),
                           ("/controls/nose/correct", {"went": "left"})):
            assert client.post(path, json=body, headers=auth(client)).status_code in (404, 405)

    def test_choosing_the_room_frame_is_kept(self, client):
        body = client.post("/controls/frame", json={"frame": "room"}, headers=auth(client)).json()
        assert body["controls"]["key_frame"] == "room"
        again = client.get("/session", headers=auth(client)).json()
        assert again["controls"]["key_frame"] == "room"

    def test_an_unknown_frame_is_a_readable_refusal(self, client):
        response = client.post("/controls/frame", json={"frame": "nose"}, headers=auth(client))
        assert response.status_code == 409 and "operator" in response.json()["detail"]

    def test_marking_and_clearing_a_spot(self, client):
        body = client.post("/controls/operator", json={"x": 1.25, "y": -0.5},
                           headers=auth(client)).json()
        assert body["controls"]["operator"] == [1.25, -0.5]
        assert body["controls"]["operator_marked_at"]
        body = client.post("/controls/operator", json={"clear": True}, headers=auth(client)).json()
        assert body["controls"]["operator"] is None

    def test_from_the_drone_needs_a_drone(self, client):
        response = client.post("/controls/operator", json={"from_drone": True},
                               headers=auth(client))
        assert response.status_code == 409
        assert "not reporting a position" in response.json()["detail"]


class TestStationSpace:
    def test_no_drone_is_no_room(self, client):
        body = client.get("/station/space", headers=auth(client)).json()
        assert body["predicted"] is None
        assert body["fence_half_extent_m"] > 0

    def test_a_prediction_is_sent_whole(self, client, monkeypatch):
        from dronedeck.mission.plan.coverage import Prediction
        monkeypatch.setattr(rest.agent.session, "manual_space",
                            lambda: Prediction(0.1, 1.0, ((0.5, ((0, 0), (1, 0), (0, 1))),), None))
        body = client.get("/station/space", headers=auth(client)).json()
        assert body["predicted"]["z_min_m"] == 0.1
        assert body["predicted"]["slices"][0]["outline"] == [[0, 0], [1, 0], [0, 1]]

    def test_it_needs_the_agent_token(self, client):
        assert client.get("/station/space").status_code in (401, 403)


class TestSetupCommand:
    def test_the_page_gets_the_command_for_this_machine(self, client):
        body = client.get("/setup/command", headers=auth(client)).json()
        assert body["command"].endswith("setup")
        assert body["check"] == body["command"] + " --check"

    def test_it_needs_the_agent_token(self, client):
        assert client.get("/setup/command").status_code in (401, 403)


class TestSetupWritesNothing:
    def test_there_is_no_install_route(self, client):
        """Installing is a terminal command only: no route may flash the drone."""
        assert client.post("/setup/install", headers=auth(client)).status_code in (404, 405)
        assert client.post("/setup/install", json={"reinstall": ["camera"]},
                           headers=auth(client)).status_code in (404, 405)
