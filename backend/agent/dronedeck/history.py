"""The session history kept on this laptop.

Every session leaves a folder the desktop app can read back, online or not:

    sessions/<session id>/meta.json     who, which drone, when, and a summary
    sessions/<session id>/samples.csv   every stream variable, once a second,
                                        for the WHOLE session — not only while
                                        a flight is recording

The outbox is the wrong place for this: it deletes records once Supabase has
them, and the operator's own history should not disappear because the upload
succeeded. The flight CSVs are the wrong place too: they cover flights only, and
a session's checks, idling and aborted takeoffs happen outside them.

**Auto and Manual are kept apart.** The mode can change between flights inside
one session, so it is recorded on every sample, not just on the session: a
filter on "Manual" then shows manual readings only, even from a session that
started in Auto. `modes` lists every mode a session used.

One sample a second keeps a long session small (about 1 MB an hour) while still
answering "what did the battery do between 14:02 and 14:09". The flight CSVs
keep the full 10 Hz record for the dashboard.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dronedeck.paths import data_dir

log = logging.getLogger(__name__)

SAMPLE_PERIOD_S = 1.0
MAX_SAMPLES_RETURNED = 5000


def sessions_dir() -> Path:
    root = data_dir() / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class FlightSummary:
    id: str
    mode: str
    program: str | None
    started_at: str
    ended_at: str | None = None
    outcome: str | None = None
    abort_reason: str | None = None
    #: The data pipeline for this flight (dronedeck/processing.py): None when
    #: processing was off as it began; "pending" until it lands; then
    #: "queued", "running", "done" or "failed".
    processing: str | None = None
    processing_error: str | None = None


@dataclass
class SessionSummary:
    """What the Control page's recent-sessions list shows at a glance."""

    samples: int = 0
    battery_start_v: float | None = None
    battery_end_v: float | None = None
    battery_min_v: float | None = None
    max_height_m: float | None = None
    temp_min_c: float | None = None
    temp_max_c: float | None = None


@dataclass
class SessionMeta:
    id: str
    operator_id: str
    operator_email: str
    operator_name: str | None
    drone_hardware_id: str | None
    mode: str
    assisted: bool
    started_at: str
    ended_at: str | None = None
    end_reason: str | None = None
    #: Every mode this session was in at some point, in first-used order.
    modes: list[str] = field(default_factory=list)
    #: "flight", or "capture" — a Capture-page session that never flew.
    #: Sessions recorded before 2026-10-24 have none: read as "flight".
    purpose: str = "flight"
    #: Why the checks found no position (ReadyReport.unassisted_reason), as the
    #: operator was told it — None when assisted. Kept so a session that flew
    #: on the barometer says why afterwards (2026-10-24: one did, and nothing
    #: on disk said whether the stations were off, unseen or not detected).
    unassisted_reason: str | None = None
    flights: list[FlightSummary] = field(default_factory=list)
    summary: SessionSummary = field(default_factory=SessionSummary)
    #: The session's own processing (its samples around the flights): queued,
    #: running, done, failed — None when it was never processed.
    processing: str | None = None
    processing_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SessionMeta:
        data = dict(raw)
        data["flights"] = [FlightSummary(**f) for f in data.get("flights", [])]
        data["summary"] = SessionSummary(**data.get("summary", {}))
        return cls(**data)

    @property
    def duration_s(self) -> float | None:
        if self.ended_at is None:
            return None
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.ended_at)
        return (end - start).total_seconds()


class SessionLog:
    """One session's folder. Written as the session happens, never after."""

    def __init__(
        self,
        meta: SessionMeta,
        *,
        root: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        height_reference: Callable[[], float | None] = lambda: None,
    ) -> None:
        self.meta = meta
        self.folder = (root or sessions_dir()) / meta.id
        self.folder.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._height_reference = height_reference
        self._lock = threading.Lock()
        self._last_sample: float | None = None
        self._columns: list[str] | None = None
        self._file: Any = None
        self._writer: Any = None
        if meta.mode not in meta.modes:
            meta.modes.append(meta.mode)
        self._save()

    # ── meta ─────────────────────────────────────────────────────────────

    def _save(self) -> None:
        """Atomic: a crash mid-write leaves the previous meta, never half of one."""
        target = self.folder / "meta.json"
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(self.meta.to_dict(), indent=2))
        os.replace(temp, target)

    def flight_started(self, flight_id: str, mode: str, program: str | None, *,
                       processing: bool = False) -> None:
        with self._lock:
            self.meta.flights.append(
                FlightSummary(id=flight_id, mode=mode, program=program, started_at=_now_iso(),
                              processing="pending" if processing else None))
            self._save()

    def has_flight(self, flight_id: str) -> bool:
        with self._lock:
            return any(f.id == flight_id for f in self.meta.flights)

    def flight_processing(self, flight_id: str, state: str, error: str | None = None) -> None:
        with self._lock:
            for flight in self.meta.flights:
                if flight.id == flight_id:
                    flight.processing = state
                    flight.processing_error = error
            self._save()

    def flight_finished(self, flight_id: str, *, outcome: str | None,
                        abort_reason: str | None = None) -> None:
        with self._lock:
            for flight in self.meta.flights:
                if flight.id == flight_id:
                    flight.ended_at = _now_iso()
                    flight.outcome = outcome
                    flight.abort_reason = abort_reason
            self._save()

    def set_positioning(self, assisted: bool, reason: str | None) -> None:
        """The checks ran again in this session (retry): record what they found."""
        with self._lock:
            self.meta.assisted = assisted
            self.meta.unassisted_reason = None if assisted else reason
            self._save()

    def set_mode(self, mode: str) -> None:
        with self._lock:
            self.meta.mode = mode
            if mode not in self.meta.modes:
                self.meta.modes.append(mode)
            self._save()

    # ── samples ──────────────────────────────────────────────────────────

    def sample(self, snap: Any) -> None:
        """Called at the stream rate; keeps one sample per SAMPLE_PERIOD_S."""
        now = self._clock()
        with self._lock:
            if self._last_sample is not None and now - self._last_sample < SAMPLE_PERIOD_S:
                return
            self._last_sample = now
            values: Mapping[str, float] = dict(getattr(snap, "values", {}) or {})
            if not values:
                return
            if self._writer is None:
                self._columns = ["recorded_at", "mode", "height_m", *sorted(values)]
                self._file = (self.folder / "samples.csv").open("w", newline="")
                self._writer = csv.writer(self._file)
                self._writer.writerow(self._columns)

            reference = self._height_reference()
            z = values.get("stateEstimate.z")
            height = None if z is None or reference is None else round(z - reference, 3)
            assert self._columns is not None
            row = [_now_iso(), self.meta.mode, height, *(values.get(c) for c in self._columns[3:])]
            self._writer.writerow(row)
            self._file.flush()
            self._update_summary(values, height)

    def _update_summary(self, values: Mapping[str, float], height: float | None) -> None:
        s = self.meta.summary
        s.samples += 1
        vbat = values.get("pm.vbat")
        if vbat is not None:
            if s.battery_start_v is None:
                s.battery_start_v = round(vbat, 2)
            s.battery_end_v = round(vbat, 2)
            lowest = vbat if s.battery_min_v is None else min(s.battery_min_v, vbat)
            s.battery_min_v = round(lowest, 2)
        if height is not None:
            highest = height if s.max_height_m is None else max(s.max_height_m, height)
            s.max_height_m = round(highest, 3)
        temp = values.get("baro.temp")
        if temp is not None:
            s.temp_min_c = round(temp if s.temp_min_c is None else min(s.temp_min_c, temp), 2)
            s.temp_max_c = round(temp if s.temp_max_c is None else max(s.temp_max_c, temp), 2)
        # Meta on disk every ten samples: cheap, and a crash loses ten seconds.
        if s.samples % 10 == 1:
            self._save()

    # ── end ──────────────────────────────────────────────────────────────

    def close(self, reason: str) -> None:
        with self._lock:
            self.meta.ended_at = _now_iso()
            self.meta.end_reason = reason
            self._save()
            if self._file is not None:
                self._file.close()
                self._file, self._writer = None, None


# ── reading ──────────────────────────────────────────────────────────────


def _used(meta: SessionMeta, mode: str | None) -> bool:
    return mode is None or mode in (meta.modes or [meta.mode])


def list_sessions(
    limit: int = 20, *, mode: str | None = None, root: Path | None = None,
) -> list[dict[str, Any]]:
    """Newest first, optionally only sessions that were in `mode` at some point.

    A folder with an unreadable meta is skipped, not fatal.
    """
    folder = root or sessions_dir()
    metas: list[SessionMeta] = []
    for meta_file in folder.glob("*/meta.json"):
        try:
            metas.append(SessionMeta.from_dict(json.loads(meta_file.read_text())))
        except (OSError, ValueError, TypeError):
            log.warning("skipping unreadable session record %s", meta_file.parent.name)
    metas = [m for m in metas if _used(m, mode)]
    metas.sort(key=lambda m: m.started_at, reverse=True)
    return [_public(m) for m in metas[:limit]]


def read_session(session_id: str, *, root: Path | None = None) -> dict[str, Any] | None:
    meta_file = _session_folder(session_id, root) / "meta.json"
    if not meta_file.exists():
        return None
    return _public(SessionMeta.from_dict(json.loads(meta_file.read_text())))


def read_samples(
    session_id: str,
    variables: Iterable[str],
    *,
    mode: str | None = None,
    root: Path | None = None,
    limit: int = MAX_SAMPLES_RETURNED,
) -> list[dict[str, Any]]:
    """The requested columns, oldest first, optionally only readings taken in
    `mode`. Unknown variables come back None."""
    folder = _session_folder(session_id, root)
    path = folder / "samples.csv"
    if not path.exists():
        return []
    fallback_mode = None
    meta_file = folder / "meta.json"
    if meta_file.exists():
        fallback_mode = json.loads(meta_file.read_text()).get("mode")
    wanted = list(variables)
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as f:
        for raw in csv.DictReader(f):
            row_mode = raw.get("mode") or fallback_mode
            if mode is not None and row_mode != mode:
                continue
            row: dict[str, Any] = {"recorded_at": raw.get("recorded_at"), "mode": row_mode}
            for name in wanted:
                row[name] = _number(raw.get(name))
            rows.append(row)
    # Evenly thinned, so a long session still spans its whole time range.
    if len(rows) > limit:
        step = len(rows) / limit
        rows = [rows[int(i * step)] for i in range(limit)]
    return rows


#: Processing states a closed app can leave behind: the flight was waiting,
#: queued or being processed when the agent stopped.
INTERRUPTED = ("pending", "queued", "running")


def interrupted_flights(*, root: Path | None = None) -> list[str]:
    """Flights on this laptop whose processing never finished — the app closed
    first. Oldest first. A flight that FAILED is not among them: it failed for
    a reason, and running it again unasked would fail the same way each time
    the app starts; the operator can process it again from the app."""
    found: list[tuple[str, str]] = []
    for meta_file in (root or sessions_dir()).glob("*/meta.json"):
        try:
            raw = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        for flight in raw.get("flights") or []:
            if flight.get("processing") in INTERRUPTED and flight.get("id"):
                found.append((str(flight.get("started_at") or ""), str(flight["id"])))
    return [flight_id for _, flight_id in sorted(found)]


def recorded_flights(*, root: Path | None = None) -> list[str]:
    """Every flight the sessions on this laptop recorded, oldest first —
    what `dronedeck process --all` walks."""
    found: list[tuple[str, str]] = []
    for meta_file in (root or sessions_dir()).glob("*/meta.json"):
        try:
            raw = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        for flight in raw.get("flights") or []:
            if flight.get("id"):
                found.append((str(flight.get("started_at") or ""), str(flight["id"])))
    return [flight_id for _, flight_id in sorted(found)]


def session_of_flight(flight_id: str, *, root: Path | None = None) -> str | None:
    """The session on this laptop that recorded a flight, or None."""
    for meta_file in (root or sessions_dir()).glob("*/meta.json"):
        try:
            raw = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        if any(f.get("id") == flight_id for f in raw.get("flights") or []):
            return str(raw.get("id") or meta_file.parent.name)
    return None


#: One writer at a time for closed sessions' meta.json. A processing job
#: reports "queued" from the caller's thread and "running"/"done" from the
#: queue's worker; two unguarded read-modify-writes through one temp file
#: interleaved and left a meta.json that was not JSON (found 2026-10-09 by the
#: resume tests, 2 runs in 15).
_CLOSED_META_LOCK = threading.Lock()


def _write_meta(meta_file: Path, raw: Mapping[str, Any]) -> None:
    """A temp file of its own, then an atomic rename: a crash mid-write leaves
    the previous meta, never half of one. Caller holds _CLOSED_META_LOCK."""
    fd, temp = tempfile.mkstemp(dir=meta_file.parent, prefix=".meta.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(raw, indent=2))
        os.replace(temp, meta_file)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def set_session_processing(session_id: str, state: str, error: str | None = None, *,
                           root: Path | None = None) -> bool:
    """Record a CLOSED session's own processing state. False when this laptop
    has no such session."""
    meta_file = (root or sessions_dir()) / session_id / "meta.json"
    with _CLOSED_META_LOCK:
        try:
            raw = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            return False
        raw["processing"], raw["processing_error"] = state, error
        _write_meta(meta_file, raw)
        return True


def interrupted_sessions(*, root: Path | None = None) -> list[str]:
    """Closed sessions whose own processing never finished — the app closed
    first. Oldest first; a FAILED one is left for the operator, as a flight is."""
    found: list[tuple[str, str]] = []
    for meta_file in (root or sessions_dir()).glob("*/meta.json"):
        try:
            raw = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        if raw.get("ended_at") and raw.get("processing") in INTERRUPTED and raw.get("id"):
            found.append((str(raw.get("started_at") or ""), str(raw["id"])))
    return [session_id for _, session_id in sorted(found)]


def recorded_sessions(*, root: Path | None = None) -> list[str]:
    """Every CLOSED session on this laptop that recorded samples, oldest first —
    what `dronedeck process --all` walks besides the flights."""
    found: list[tuple[str, str]] = []
    for meta_file in (root or sessions_dir()).glob("*/meta.json"):
        try:
            raw = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        if raw.get("ended_at") and raw.get("id") and \
                (meta_file.parent / "samples.csv").exists():
            found.append((str(raw.get("started_at") or ""), str(raw["id"])))
    return [session_id for _, session_id in sorted(found)]


def set_flight_processing(flight_id: str, state: str, error: str | None = None, *,
                          root: Path | None = None) -> bool:
    """Record a flight's processing state in whichever CLOSED session holds it.
    An open session is written through its SessionLog instead — it keeps the
    meta in memory and would overwrite a change made to the file under it.
    False when no session on this laptop has the flight."""
    with _CLOSED_META_LOCK:
        for meta_file in (root or sessions_dir()).glob("*/meta.json"):
            try:
                raw = json.loads(meta_file.read_text())
            except (OSError, ValueError):
                continue
            flights = raw.get("flights") or []
            if not any(f.get("id") == flight_id for f in flights):
                continue
            for flight in flights:
                if flight.get("id") == flight_id:
                    flight["processing"], flight["processing_error"] = state, error
            _write_meta(meta_file, raw)
            return True
        return False


def session_frames(session_id: str, source: str | None = None, *,
                   root: Path | None = None) -> list[dict[str, Any]]:
    """A recorded session's camera frames — of one source ("manual": the
    captures made by hand), oldest first. Raises ValueError for a bad id."""
    from dronedeck.camera.recording import read_index
    return read_index(_session_folder(session_id, root), source)


def session_frame(session_id: str, seq: int, *,
                  root: Path | None = None) -> tuple[Path, str] | None:
    """One recorded frame of a session, by number. Raises ValueError for a bad id."""
    from dronedeck.camera.recording import frame_file
    return frame_file(_session_folder(session_id, root), seq)


def _session_folder(session_id: str, root: Path | None) -> Path:
    # The id arrives from a URL: refuse anything that could walk out of the folder.
    if not session_id or any(c in session_id for c in "/\\."):
        raise ValueError("invalid session id")
    return (root or sessions_dir()) / session_id


def _number(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _public(meta: SessionMeta) -> dict[str, Any]:
    data = meta.to_dict()
    data["duration_s"] = meta.duration_s
    return data
