"""Getting local records to Supabase, eventually and exactly once.

The laptop is the one that must push: no server can reach into it, the same
constraint as the radio. So this runs whenever it can — during a flight, when a
flight ends, at sign-in, and every minute while anything is unsent — and the
desktop app stays in the tray until it reports nothing pending.

Order matters, because records reference each other: drones, then sessions,
then flights, then each flight's telemetry and log file. Audit events have no
foreign keys precisely so they can go first and never wait for anything.

Nothing is ever sent twice into a duplicate: ids are generated on the laptop,
records upsert on their id, and telemetry upserts on ``(flight_id, index)``
after asking the server for the highest row it already holds.

File storage is a separate resource from the database, with its own quota.
The first file upload storage refuses in a pass skips the rest of that pass's
file uploads, and the database rows go on without them: the files wait on
this computer, cursors unmoved, and are tried again on the next pass.
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ParamSpec, get_args, get_type_hints

from dronedeck import playback
from dronedeck.camera.recording import INDEX_NAME, frames_to_upload
from dronedeck.paths import data_dir
from dronedeck.sync import flight_playback, results, samples
from dronedeck.sync.cloud import Cloud, CloudError, CloudRefused, StorageError
from dronedeck.sync.outbox import Kind, Outbox
from dronedeck.telemetry.row import TelemetryRow

log = logging.getLogger(__name__)

AUDIT_BATCH = 100
TELEMETRY_BATCH = 500
SYNC_INTERVAL_S = 60.0

STORAGE_FULL = ("Supabase file storage is full. Readings and results still upload; "
                "photos and flight logs wait on this computer until space is freed.")

_P = ParamSpec("_P")


def storage_message(error: StorageError) -> str:
    """What the window says when file storage refused an upload."""
    if error.full:
        return STORAGE_FULL
    return (f"Photos and flight logs did not upload ({error}). They wait on this "
            "computer and are tried again.")

# Column → type, from the one row schema shared by the CSV and Postgres.
#
# Resolved with get_type_hints, not the raw annotations: `from __future__ import
# annotations` makes those plain strings, and comparing a string to `int` never
# matches — every column came back as text, so `index + 1` concatenated.
def _column_types() -> dict[str, type]:
    types: dict[str, type] = {}
    for name, hint in get_type_hints(TelemetryRow).items():
        args = [a for a in get_args(hint) if a is not type(None)]
        resolved = args[0] if args else hint
        if resolved in (int, float, str):
            types[name] = resolved
    return types


_TYPES = _column_types()


def close_orphaned_flights(outbox: Outbox) -> list[str]:
    """Close every flight record a previous run of the agent left open.

    A flight's record is ended when it lands (Session._finish_flight). An app
    closed — or killed — mid-flight never gets there, so the record stays
    "still flying" for ever: the syncer keeps polling its telemetry, never
    uploads its CSV, and its pipeline result waits behind it (eight such
    flights from the 2026-09-22 bring-up, found 2026-10-09). Called once as
    the agent starts, before any flight can begin, so every open record is
    an orphan. Ended at the CSV's last row (else when it began), marked
    aborted, with the rows the CSV holds. Returns the flights closed."""
    closed = []
    for record in outbox.pending(Kind.FLIGHT):
        if record.payload.get("ended_at"):
            continue
        ended_at, rows = record.payload.get("started_at"), 0
        csv_path = Path(record.payload.get("csv_path") or "")
        if csv_path.is_file():
            try:
                with csv_path.open(newline="") as handle:
                    for row in csv.DictReader(handle):
                        rows += 1
                        ended_at = row.get("recorded_at") or ended_at
            except (OSError, csv.Error):
                log.warning("flight %s: its CSV could not be read; closing it as begun",
                            record.id)
        outbox.update(Kind.FLIGHT, record.id, lambda p, e=ended_at, n=rows: p.update({
            "ended_at": e, "status": "aborted", "outcome": "interrupted",
            "error": "The app closed while this flight was recording.",
            "rows_written": n,
        }))
        closed.append(record.id)
    if closed:
        log.info("closed %d flight record(s) the last run left open", len(closed))
    return closed


def parse_csv_row(raw: dict[str, str]) -> dict[str, Any]:
    """One CSV line back into the types Postgres expects."""
    row: dict[str, Any] = {}
    for column, value in raw.items():
        if value == "" or value is None:
            row[column] = None
            continue
        kind = _TYPES.get(column, str)
        if kind is int:
            row[column] = int(float(value))
        elif kind is float:
            row[column] = float(value)
        else:
            row[column] = value
    return row


def read_rows_after(csv_path: Path, after_index: int | None) -> Iterator[dict[str, Any]]:
    """Rows from the flight's CSV with an index above `after_index`."""
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            index = raw.get("index")
            if index in (None, ""):
                continue
            if after_index is not None and int(float(index)) <= after_index:
                continue
            yield parse_csv_row(raw)


@dataclass
class SyncStatus:
    """What the operator sees before they close the app."""

    pending_flights: int = 0
    pending_events: int = 0
    uploading: str | None = None
    last_error: str | None = None
    last_success_at: float | None = None
    #: Set while file storage refuses uploads, from the last pass that tried
    #: one; cleared by the first pass whose file uploads go through.
    storage_error: str | None = None
    storage_full: bool = False

    @property
    def everything_sent(self) -> bool:
        return self.pending_flights == 0 and self.pending_events == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_flights": self.pending_flights,
            "pending_events": self.pending_events,
            "uploading": self.uploading,
            "last_error": self.last_error,
            "everything_sent": self.everything_sent,
            "storage_error": self.storage_error,
            "storage_full": self.storage_full,
        }


class Syncer:
    def __init__(
        self,
        outbox: Outbox,
        cloud_for: Callable[[], Cloud | None],
        *,
        on_status: Callable[[SyncStatus], None] | None = None,
        interval_s: float = SYNC_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
        data_root: Path | None = None,
    ) -> None:
        """`cloud_for` returns the signed-in client, or None when signed out.
        `data_root` is where a flight's files are found (paths.data_dir())."""
        self._outbox = outbox
        self._data_root = data_root
        self._cloud_for = cloud_for
        self._on_status = on_status
        self._interval = interval_s
        self._sleep = sleep
        self.status = SyncStatus()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        # This pass's file storage: the refusal that closed it, and whether a
        # file upload was tried at all (a pass with none says nothing new).
        self._storage_refused: StorageError | None = None
        self._storage_tried = False

    # ── loop ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is None:
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True, name="sync")
            self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def trigger(self) -> None:
        """Sync now — called at flight end, sign-in and End session."""
        self._wake.set()

    def _run(self) -> None:
        while self._running:
            try:
                self.sync_once()
            except Exception:
                log.exception("sync pass failed; will retry")
            self._wake.wait(self._interval)
            self._wake.clear()

    # ── one pass ─────────────────────────────────────────────────────────

    def sync_once(self) -> SyncStatus:
        with self._lock:
            cloud = self._cloud_for()
            if cloud is None:
                self._refresh_counts(offline=True)
                return self.status
            self._storage_refused, self._storage_tried = None, False
            try:
                self._send_audit(cloud)
                self._send_drones(cloud)
                self._send_sessions(cloud)
                self._send_flights(cloud)
                self._send_frames(cloud)
                # Last: until the database has session_samples (migration
                # 20261006000013) this one fails, and everything above has
                # already gone.
                self._send_samples(cloud)
                # After that, for the same reason: pipeline results need
                # 20261009000016, a session's own result 20261009000018.
                self._send_results(cloud)
                self._send_session_results(cloud)
                # Last of all: playback's plan and trace need migration
                # 20261011000019, and nothing waits on them.
                self._send_playback(cloud)
                self.status.last_error = None
                self.status.last_success_at = time.time()
            except CloudError as e:
                # Expected when offline: everything stays on disk for next time.
                self.status.last_error = str(e)
                log.info("sync incomplete: %s", e)
            finally:
                self.status.uploading = None
                self._note_storage()
                self._refresh_counts()
            return self.status

    def _note_storage(self) -> None:
        """Carry this pass's file storage into the status. The raw refusal is
        logged when storage first starts refusing, not every pass: its shape
        on a full project is not documented, and this is how it gets seen."""
        refused = self._storage_refused
        if refused is not None:
            if self.status.storage_error is None:
                log.warning("file storage refused an upload (%r); files wait on this "
                            "computer, rows go on", refused.__cause__ or refused)
            self.status.storage_error = storage_message(refused)
            self.status.storage_full = refused.full
        elif self._storage_tried:
            if self.status.storage_error is not None:
                log.info("file storage accepts uploads again")
            self.status.storage_error = None
            self.status.storage_full = False

    def _store(self, upload: Callable[_P, None], *args: _P.args, **kwargs: _P.kwargs) -> bool:
        """One file upload, unless storage already refused one this pass.

        False when it did not happen: the caller leaves its cursor where it
        is and goes on to its rows. Only a StorageError is held here — a
        database error still ends the pass as before."""
        if self._storage_refused is not None:
            return False
        self._storage_tried = True
        try:
            upload(*args, **kwargs)
        except StorageError as e:
            self._storage_refused = e
            log.info("file uploads paused for this pass: %s", e)
            return False
        return True

    def _refresh_counts(self, offline: bool = False) -> None:
        self.status.pending_flights = self._outbox.count_pending(Kind.FLIGHT)
        self.status.pending_events = self._outbox.count_pending(Kind.AUDIT)
        if offline:
            self.status.last_error = "Not signed in — records are saved on this computer."
        if self._on_status is not None:
            try:
                self._on_status(self.status)
            except Exception:
                log.exception("sync status callback failed")

    # ── each kind ────────────────────────────────────────────────────────

    def _send_audit(self, cloud: Cloud) -> None:
        batch: list[dict[str, Any]] = []
        ids: list[str] = []
        for record in self._outbox.pending(Kind.AUDIT):
            batch.append({k: v for k, v in record.payload.items() if not k.startswith("_")})
            ids.append(record.id)
            if len(batch) >= AUDIT_BATCH:
                break
        if not batch:
            return
        cloud.insert_audit(batch)
        for event_id in ids:
            self._outbox.remove(Kind.AUDIT, event_id)

    def _send_drones(self, cloud: Cloud) -> None:
        for record in self._outbox.pending(Kind.DRONE):
            payload = record.payload
            drone_id = cloud.upsert_drone(
                payload["hardware_id"], payload.get("name") or payload["hardware_id"],
                payload.get("uri", ""),
            )
            self._outbox.update(
                Kind.DRONE, record.id,
                lambda payload, drone_id=drone_id: payload.update(
                    {"drone_id": drone_id, "_sent": True}),
            )

    def _send_sessions(self, cloud: Cloud) -> None:
        for record in self._outbox.pending(Kind.SESSION):
            row = self._row(record.payload)
            row["drone_id"] = self._drone_id(record.payload.get("drone_hardware_id"))
            cloud.upsert_session(row)
            # Only finished sessions are done; an open one is re-sent as it changes.
            if record.payload.get("ended_at"):
                self._outbox.mark_sent(Kind.SESSION, record.id)

    def _send_flights(self, cloud: Cloud) -> None:
        for record in self._outbox.pending(Kind.FLIGHT):
            payload = record.payload
            flight_id = record.id
            row = self._row(payload, drop={
                "csv_path", "csv_object", "csv_uploaded", "rows_written", "drone_hardware_id"})
            row["drone_id"] = self._drone_id(payload.get("drone_hardware_id"))
            cloud.upsert_flight(row)

            csv_path = Path(payload.get("csv_path", ""))
            if not csv_path.exists():
                log.warning("flight %s has no CSV on disk; sending the record only", flight_id)
                if payload.get("ended_at"):
                    self._outbox.mark_sent(Kind.FLIGHT, flight_id)
                continue

            self.status.uploading = flight_id
            sent_to = cloud.max_telemetry_index(flight_id)
            batch: list[dict[str, Any]] = []
            for telemetry_row in read_rows_after(csv_path, sent_to):
                batch.append(telemetry_row)
                if len(batch) >= TELEMETRY_BATCH:
                    cloud.insert_telemetry(batch)
                    sent_to = batch[-1]["index"]
                    batch = []
            if batch:
                cloud.insert_telemetry(batch)
                sent_to = batch[-1]["index"]

            if not payload.get("ended_at"):
                continue        # still flying: rows keep coming

            expected = payload.get("rows_written")
            # A flight that wrote no rows is complete once its record is up —
            # otherwise it is retried every pass, forever.
            complete = not expected or (sent_to is not None and sent_to + 1 >= expected)
            if complete and not payload.get("_rows_sent"):
                # What results and playback wait on: the row and its telemetry
                # are up, whether or not the log file has followed yet.
                self._outbox.update(Kind.FLIGHT, flight_id,
                                    lambda p: p.__setitem__("_rows_sent", True))

            object_path = payload.get("csv_object") or f"{flight_id}.csv.gz"
            if not payload.get("csv_uploaded"):
                if not self._store(cloud.upload_flight_csv, object_path, csv_path):
                    continue    # storage refused: the log waits here, the record stays pending
                self._outbox.update(
                    Kind.FLIGHT, flight_id,
                    lambda payload, object_path=object_path: payload.update(
                        {"csv_uploaded": True, "csv_object": object_path}),
                )
                # The server can finish the import even if this laptop closes now.
                cloud.request_backfill(flight_id, object_path)

            if complete:
                self._outbox.mark_sent(Kind.FLIGHT, flight_id)
            else:
                log.info("flight %s: %s of %s rows uploaded", flight_id,
                         (sent_to or -1) + 1, expected)

    def _send_frames(self, cloud: Cloud) -> None:
        """A session's recorded frames, from its cursor on (camera/recording.py).

        Only frames frames.csv marks for upload; the cursor advances after each
        one lands, so an interrupted pass resumes exactly where it stopped. The
        index goes last, once the session has ended — replacing any earlier
        copy — and only then is the record sent. When storage refuses, the
        record keeps its cursor and waits for a later pass.
        """
        for record in self._outbox.pending(Kind.FRAMES):
            payload = record.payload
            session_id = record.id
            folder = Path(payload.get("folder", ""))
            if not folder.exists():
                log.warning("session %s has no frames folder; nothing to upload", session_id)
                self._outbox.mark_sent(Kind.FRAMES, session_id)
                continue
            self.status.uploading = f"frames {session_id[:8]}"
            cursor = int(payload.get("uploaded_through", 0))
            stored = True
            for seq, path, content_type in frames_to_upload(folder, cursor):
                stored = self._store(cloud.upload_frame,
                                     f"{session_id}/{path.parent.name}/{path.name}",
                                     path, content_type)
                if not stored:
                    break
                self._outbox.update(Kind.FRAMES, session_id,
                                    lambda p, seq=seq: p.__setitem__("uploaded_through", seq))
            if not stored or not payload.get("ended"):
                continue        # storage refused, or still recording: more frames will come
            if self._store(cloud.upload_frame, f"{session_id}/{INDEX_NAME}",
                           folder / INDEX_NAME, "text/csv", replace=True):
                self._outbox.mark_sent(Kind.FRAMES, session_id)

    def _send_samples(self, cloud: Cloud) -> None:
        """A session's 1 Hz vitals, from its cursor on (sync/samples.py).

        The cursor advances after each batch lands, so an interrupted pass
        resumes where it stopped. Sent once the session has ended and every
        row is up — the session row itself goes first (_send_sessions), which
        the table's foreign key and row-level security both need."""
        for record in self._outbox.pending(Kind.SAMPLES):
            payload = record.payload
            session_id = record.id
            folder = Path(payload.get("folder", ""))
            self.status.uploading = f"vitals {session_id[:8]}"
            cursor = int(payload.get("uploaded_through", 0))
            for batch in samples.batches(folder, session_id, cursor):
                cloud.insert_session_samples(batch)
                last = batch[-1]["seq"]
                self._outbox.update(Kind.SAMPLES, session_id,
                                    lambda p, last=last: p.__setitem__("uploaded_through", last))
            if payload.get("ended"):
                self._outbox.mark_sent(Kind.SAMPLES, session_id)

    def _send_results(self, cloud: Cloud) -> None:
        """A processed flight's result, findings and enhanced frames
        (sync/results.py).

        Waits until the flight's row and telemetry are up: the result's
        foreign key needs the flight row (not its log file, which may be
        waiting on storage). Enhanced frames go first, from a cursor
        (replacing — a re-process may change them), then the result, then the
        findings, then the findings this result no longer makes are deleted.
        When storage refuses a frame, the rows go anyway, once, and the record
        waits for its frames. Re-processing re-queues the record, which resets
        the cursor and the rows."""
        for record in self._outbox.pending(Kind.RESULTS):
            payload = record.payload
            flight_id = record.id
            flight = self._outbox.get(Kind.FLIGHT, flight_id)
            if flight is not None and not self._flight_rows_up(flight):
                continue            # the flight row first
            folder = Path(payload.get("folder", ""))
            try:
                result = results.load(folder)
            except results.ResultUnreadable as e:
                log.warning("result of flight %s cannot be uploaded: %s", flight_id, e)
                self._outbox.mark_sent(Kind.RESULTS, flight_id)
                continue
            self.status.uploading = f"result {flight_id[:8]}"
            session_id = result.get("session_id")
            frames_up = not session_id or self._store_enhanced(
                cloud, Kind.RESULTS, flight_id, session_id, folder, result, payload)
            if not payload.get("_rows_sent"):
                cloud.upsert_result(results.result_row(result))
                rows = results.finding_rows(result)
                cloud.upsert_findings(rows)
                cloud.delete_findings_except(flight_id, [r["id"] for r in rows])
            if frames_up:
                self._outbox.mark_sent(Kind.RESULTS, flight_id)
            else:
                self._outbox.update(Kind.RESULTS, flight_id,
                                    lambda p: p.__setitem__("_rows_sent", True))

    def _send_playback(self, cloud: Cloud) -> None:
        """A flight's plan as flown and its control trace (sync/flight_playback.py),
        so it plays back on any computer.

        Waits while the flight's own record is pending: both tables' foreign
        key and policy need the flight row. The plan goes once; the trace from
        its cursor, a batch at a time. A record Supabase refuses by policy (a
        flight another account flew) is given up — it would be refused every
        pass and hold the queue behind it — and so is one whose files are
        gone: there is nothing left to send."""
        root = self._data_root or data_dir()
        for record in self._outbox.pending(Kind.PLAYBACK):
            flight_id = record.id
            flight = self._outbox.get(Kind.FLIGHT, flight_id)
            if flight is None:
                log.warning("flight %s has no flight record here; its plan and trace "
                            "cannot be uploaded", flight_id)
                self._outbox.mark_sent(Kind.PLAYBACK, flight_id)
                continue
            if not self._flight_rows_up(flight):
                continue            # the flight row first
            try:
                files = playback.files_of(root, flight_id)
            except playback.PlaybackError:
                log.warning("flight %s has no files on this computer; nothing to upload "
                            "for playback", flight_id)
                self._outbox.mark_sent(Kind.PLAYBACK, flight_id)
                continue
            self.status.uploading = f"playback {flight_id[:8]}"
            try:
                if files.plan is not None and not record.payload.get("plan_sent"):
                    try:
                        kept = flight_playback.read_plan(files.plan)
                        row = flight_playback.plan_row(flight_id, kept)
                    except (OSError, ValueError, KeyError) as e:
                        log.warning("the plan of flight %s cannot be read (%s); sending "
                                    "the trace only", flight_id, e)
                    else:
                        cloud.insert_flight_plan(row)
                    self._outbox.update(Kind.PLAYBACK, flight_id,
                                        lambda p: p.__setitem__("plan_sent", True))
                if files.trace is not None:
                    cursor = int(record.payload.get("uploaded_through", 0))
                    for batch in flight_playback.trace_batches(files.trace, flight_id, cursor):
                        cloud.insert_flight_traces(batch)
                        last = batch[-1]["seq"]
                        self._outbox.update(Kind.PLAYBACK, flight_id,
                                            lambda p, last=last: p.__setitem__(
                                                "uploaded_through", last))
            except CloudRefused as e:
                log.warning("flight %s: %s; not retrying", flight_id, e)
            self._outbox.mark_sent(Kind.PLAYBACK, flight_id)

    def _send_session_results(self, cloud: Cloud) -> None:
        """A processed session's own result (sync/results.py session_result_row):
        enhanced frames from a cursor, the result, its findings, and the
        retraction of session findings a re-process no longer makes. Waits
        while the session's own record is still pending: the row's foreign key
        needs the session. When storage refuses a frame, the rows go anyway,
        once, and the record waits for its frames."""
        for record in self._outbox.pending(Kind.SESSION_RESULTS):
            payload = record.payload
            session_id = record.id
            session = self._outbox.get(Kind.SESSION, session_id)
            if session is not None and not session.get("_sent"):
                continue            # the session row first
            folder = Path(payload.get("folder", ""))
            try:
                result = results.load(folder)
            except results.ResultUnreadable as e:
                log.warning("result of session %s cannot be uploaded: %s", session_id, e)
                self._outbox.mark_sent(Kind.SESSION_RESULTS, session_id)
                continue
            self.status.uploading = f"session result {session_id[:8]}"
            frames_up = self._store_enhanced(
                cloud, Kind.SESSION_RESULTS, session_id, session_id, folder, result, payload)
            if not payload.get("_rows_sent"):
                cloud.upsert_session_result(results.session_result_row(result))
                rows = results.finding_rows(result)
                cloud.upsert_findings(rows)
                cloud.delete_session_findings_except(session_id, [r["id"] for r in rows])
            if frames_up:
                self._outbox.mark_sent(Kind.SESSION_RESULTS, session_id)
            else:
                self._outbox.update(Kind.SESSION_RESULTS, session_id,
                                    lambda p: p.__setitem__("_rows_sent", True))

    # ── helpers ──────────────────────────────────────────────────────────

    def _store_enhanced(self, cloud: Cloud, kind: Kind, record_id: str, session_id: str,
                        folder: Path, result: dict[str, Any], payload: dict[str, Any]) -> bool:
        """A result's enhanced frames from the record's cursor. False when
        storage refused one: the rest wait for a later pass."""
        done = int(payload.get("frames_uploaded", 0))
        for n, (seq, path) in enumerate(results.enhanced_frames(folder, result)):
            if n < done:
                continue
            if not self._store(cloud.upload_frame, results.enhanced_object_path(session_id, seq),
                               path, "image/png", replace=True):
                return False
            self._outbox.update(kind, record_id,
                                lambda p, n=n: p.__setitem__("frames_uploaded", n + 1))
        return True

    @staticmethod
    def _flight_rows_up(flight: dict[str, Any]) -> bool:
        """Whether a flight's row and telemetry are on the server — what its
        result and playback wait on. Its log file may still be waiting on
        storage (`_rows_sent` without `_sent`)."""
        return bool(flight.get("_sent") or flight.get("_rows_sent"))

    def _drone_id(self, hardware_id: str | None) -> str | None:
        if not hardware_id:
            return None
        record = self._outbox.get(Kind.DRONE, hardware_id)
        return (record or {}).get("drone_id")

    @staticmethod
    def _row(payload: dict[str, Any], drop: set[str] | None = None) -> dict[str, Any]:
        """The server-facing columns: local bookkeeping and locals-only fields out."""
        skip = {"drone_hardware_id"} | (drop or set())
        return {k: v for k, v in payload.items() if not k.startswith("_") and k not in skip}
