"""Who did what, with which drone, and what happened.

Every operator action and every automatic safety decision is recorded here, so
the web's audit page can answer "who flew this, and why did it end?" for all
time. Events are written to the outbox first and uploaded by the sync, which
means a flight with no internet is still fully accounted for.

Events are **append-only**: the database has no update or delete policy for
them, operators included.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from dronedeck.sync.outbox import Kind, Outbox, new_id


class Action(StrEnum):
    SIGN_IN = "sign_in"
    SIGN_OUT = "sign_out"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    CHECKS = "checks"
    AREA_CONFIRMED = "area_confirmed"
    PROP_TEST = "prop_test"
    HEALTH_TEST = "health_test"
    SESSION_RETRY = "session_retry"
    DRONE_RESET = "drone_reset"             # motors off, restart, checks again
    MODE_CHANGED = "mode_changed"
    PROGRAM_RUN = "program_run"
    MISSION_RUN = "mission_run"
    MANUAL_ARM = "manual_arm"
    MANUAL_FLIGHT = "manual_flight"
    LAND = "land"
    EMERGENCY_STOP = "emergency_stop"
    GUARD_ABORT = "guard_abort"
    PROCESSING_SET = "processing_set"       # the DPP switch, on or off
    PROCESSING_RUN = "processing_run"       # a flight asked for by hand


class Result(StrEnum):
    OK = "ok"
    REFUSED = "refused"        # the system declined: checks failed, wrong role
    FAILED = "failed"          # it was attempted and went wrong
    ABORTED = "aborted"        # a guard or the operator ended it early


class AuditLog:
    """Records events for one signed-in operator."""

    def __init__(
        self, outbox: Outbox, *, actor_id: str, actor_email: str, source: str = "desktop"
    ) -> None:
        self._outbox = outbox
        self._actor_id = actor_id
        self._actor_email = actor_email
        self._source = source

    def record(
        self,
        action: Action,
        result: Result = Result.OK,
        *,
        session_id: str | None = None,
        flight_id: str | None = None,
        drone_hardware_id: str | None = None,
        mission_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> str:
        event_id = new_id()
        self._outbox.put(Kind.AUDIT, event_id, {
            "id": event_id,
            "occurred_at": datetime.now(UTC).isoformat(),
            "actor_id": self._actor_id,
            "actor_email": self._actor_email,
            "source": self._source,
            "action": str(action),
            "result": str(result),
            "session_id": session_id,
            "flight_id": flight_id,
            "drone_hardware_id": drone_hardware_id,
            "mission_id": mission_id,
            "detail": detail or {},
        })
        return event_id
