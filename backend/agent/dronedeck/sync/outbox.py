"""Records waiting to reach Supabase.

Everything the agent needs to remember is written here **before** any network
call: the drone it met, the session, each flight, and every audit event. A
laptop with no internet flies exactly as well as one with internet; the records
sit in this folder until a sync succeeds, and are removed only once the server
has them.

One file per record, named by its id, written atomically (temp file + rename),
so a crash mid-write cannot leave a half-record — the same reason the CSV is
flushed every row.

Ids are generated **here**, not by the database. A flight that happens offline
already has the id its telemetry rows reference, so uploading later needs no
translation and re-uploading changes nothing (CLAUDE.md #6).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from dronedeck.paths import outbox_dir

log = logging.getLogger(__name__)


class Kind(StrEnum):
    DRONE = "drone"
    SESSION = "session"
    FLIGHT = "flight"
    AUDIT = "audit"
    #: One per session: its camera frames, uploaded from a cursor
    #: (camera/recording.py) — never one record per frame.
    FRAMES = "frames"
    #: One per session: its 1 Hz vitals (samples.csv), uploaded from a cursor
    #: (sync/samples.py) — never one record per row.
    SAMPLES = "samples"
    #: One per processed flight: its pipeline result and findings
    #: (sync/results.py). Re-processing replaces the record.
    RESULTS = "results"
    #: One per processed session: its own result — the samples around its
    #: flights (sync/results.py session_result_row).
    SESSION_RESULTS = "session_results"
    #: One per flight: its plan as flown and its control trace, for playback
    #: on any computer (sync/flight_playback.py) — the trace from a cursor.
    PLAYBACK = "playback"


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Record:
    kind: Kind
    id: str
    payload: dict[str, Any]
    path: Path

    @property
    def sent(self) -> bool:
        return bool(self.payload.get("_sent"))


class Outbox:
    """A folder of pending records. Safe to use from several threads."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or outbox_dir()
        self._lock = threading.Lock()

    def _dir(self, kind: Kind) -> Path:
        path = self.root / str(kind)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def put(self, kind: Kind, record_id: str, payload: dict[str, Any]) -> Record:
        path = self._dir(kind) / f"{record_id}.json"
        with self._lock:
            self._write(path, payload)
        return Record(kind, record_id, payload, path)

    def update(
        self, kind: Kind, record_id: str, change: Callable[..., None]
    ) -> dict[str, Any]:
        """Read-modify-write one record under the lock."""
        path = self._dir(kind) / f"{record_id}.json"
        with self._lock:
            payload = self._read(path) or {}
            change(payload)
            self._write(path, payload)
            return payload

    def get(self, kind: Kind, record_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._read(self._dir(kind) / f"{record_id}.json")

    def pending(self, kind: Kind) -> Iterator[Record]:
        """Records not yet confirmed by the server, oldest first.

        Ordered by when the record happened, not by its id: ids are random
        UUIDs, so sorting by filename would upload an audit trail out of order.
        """
        records: list[Record] = []
        for path in self._dir(kind).glob("*.json"):
            payload = self._read(path)
            if payload is None:
                continue
            record = Record(kind, path.stem, payload, path)
            if not record.sent:
                records.append(record)
        records.sort(key=lambda r: (
            str(r.payload.get("occurred_at") or r.payload.get("started_at") or ""), r.id
        ))
        yield from records

    def records(self, kind: Kind) -> Iterator[tuple[str, dict[str, Any]]]:
        """Every record of a kind, sent or not, as (id, payload). Order is not
        meaningful — use pending() for anything that must replay in order."""
        for path in self._dir(kind).glob("*.json"):
            with self._lock:
                payload = self._read(path)
            if payload is not None:
                yield path.stem, payload

    def count_pending(self, kind: Kind) -> int:
        return sum(1 for _ in self.pending(kind))

    def mark_sent(self, kind: Kind, record_id: str) -> None:
        self.update(kind, record_id, lambda payload: payload.__setitem__("_sent", True))

    def remove(self, kind: Kind, record_id: str) -> None:
        with self._lock:
            (self._dir(kind) / f"{record_id}.json").unlink(missing_ok=True)

    # ── disk ─────────────────────────────────────────────────────────────

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(temp, path)

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return payload
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            log.warning("ignoring unreadable outbox record %s", path.name)
            return None
