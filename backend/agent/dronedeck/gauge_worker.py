"""Reads the pressure-gauge photos users upload on the web.

THE FLOW (docs/features/pipeline/gauge.txt)

    browser ─ uploads photos, queues the batch ─▶ Supabase
    this worker ─ polls ─▶ claims a photo ─▶ downloads it ─▶ reads it ─▶ uploads the
                                  annotated picture and writes the reading ─▶ Supabase
    browser ─ watches the batch fill in (realtime) ─▶ shows each photo and its kPa

It runs inside the agent, signed in as the operator, exactly like the syncer:
the browser and the agent never talk to each other, only to Supabase. A photo
is read by the data pipeline (pipeline/gauge/photo.py) in a CHILD PROCESS —
`dronedeck gauge FILE --json` — at a lower priority, one at a time, for the
same reasons the flight pipeline is (processing.py): OpenCV beside the 50 Hz
flight loop competes for the interpreter lock, and a photo that crashes or
hangs the reader must never take the radio with it.

CLAIMING. A photo is claimed by an atomic update that only matches while it is
still 'pending', so two laptops never read the same one. A photo claimed and
never finished (the app closed mid-read) is put back to 'pending' after
STALE_AFTER_S and read again.

FAILING. A photo with no readable gauge is a RESULT (state 'unreadable', with
the reason), not a failure. A file that is not an image, or a reader that
crashes or times out, marks that photo 'failed' and the batch carries on.
Losing the network does neither: the photo goes back to 'pending'.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from dronedeck.processing import _cli, _lower_priority
from dronedeck.sync.cloud import CloudError

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 15.0
#: Most photos taken from the queue in one pass.
PASS_LIMIT = 25
#: A claim older than this is an agent that went away.
STALE_AFTER_S = 15 * 60
#: Longest one photo may take. A full-resolution phone photo reads in seconds.
READ_TIMEOUT_S = 120


class PhotoFailed(Exception):
    """This photo cannot be processed (not an image, reader crashed or timed out)."""


@dataclass
class Outcome:
    """What reading one photo produced: public.gauge_images' reading columns,
    and the annotated picture."""

    values: dict[str, Any]
    annotated_png: bytes | None


Reader = Callable[[bytes, str], Outcome]


class GaugeStore(Protocol):
    """The part of the cloud client this worker needs. Faked in tests."""

    def gauge_requeue_stale(self, claimed_before: str) -> None: ...
    def gauge_pending(self, limit: int) -> list[dict[str, Any]]: ...
    def gauge_claim(self, image_id: str, claimed_at: str) -> bool: ...
    def gauge_download(self, object_path: str) -> bytes: ...
    def gauge_upload(self, object_path: str, data: bytes, content_type: str) -> None: ...
    def gauge_save_image(self, image_id: str, values: dict[str, Any]) -> None: ...
    def gauge_image_statuses(self, batch_id: str) -> list[str]: ...
    def gauge_update_batch(self, batch_id: str, values: dict[str, Any]) -> None: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def annotated_path(batch_id: str, image_id: str) -> str:
    return f"{batch_id}/annotated/{image_id}.png"


def read_in_child(data: bytes, filename: str) -> Outcome:
    """The default reader: `dronedeck gauge` on a temporary copy of the photo."""
    with tempfile.TemporaryDirectory(prefix="dronedeck-gauge-") as tmp:
        folder = Path(tmp)
        source = folder / "photo"
        source.write_bytes(data)
        out = folder / "out"
        command = _cli("gauge", str(source), "--out", str(out), "--json",
                       "--label", Path(filename).stem[:60] or "photo")
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            raise PhotoFailed(f"the reader would not start ({type(e).__name__})") from e
        _lower_priority(process)
        try:
            stdout, _ = process.communicate(timeout=READ_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise PhotoFailed(f"the reader took longer than {READ_TIMEOUT_S} s") from None
        lines = stdout.decode("utf-8", "replace").splitlines()
        line = next((ln for ln in reversed(lines) if ln.startswith("{")), None)
        if line is None:
            raise PhotoFailed(f"the reader stopped without an answer (exit {process.returncode})")
        answer = json.loads(line)
        if answer.get("failed"):
            raise PhotoFailed(str(answer["failed"]))
        picture = out / "photo_read.png"
        values = {k: v for k, v in answer.items() if k not in ("file", "annotated")}
        return Outcome(values, picture.read_bytes() if picture.is_file() else None)


class GaugeWorker:
    def __init__(
        self,
        store_for: Callable[[], GaugeStore | None],
        *,
        reader: Reader = read_in_child,
        interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        """`store_for` returns the signed-in cloud client, or None when signed out."""
        self._store_for = store_for
        self._reader = reader
        self._interval = interval_s
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False

    # ── loop ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread is None:
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True, name="gauge-worker")
            self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def trigger(self) -> None:
        """Look for work now — at sign-in."""
        self._wake.set()

    def _run(self) -> None:
        while self._running:
            try:
                self.run_once()
            except Exception:
                log.exception("gauge pass failed; will retry")
            self._wake.wait(self._interval)
            self._wake.clear()

    # ── one pass ─────────────────────────────────────────────────────────

    def run_once(self) -> int:
        """Read what is waiting, oldest first. Returns how many photos were read."""
        with self._lock:
            store = self._store_for()
            if store is None:
                return 0
            try:
                stale = datetime.now(UTC) - timedelta(seconds=STALE_AFTER_S)
                store.gauge_requeue_stale(stale.isoformat())
                waiting = store.gauge_pending(PASS_LIMIT)
            except CloudError as e:
                log.info("gauge queue unavailable: %s", e)
                return 0
            read = 0
            for row in waiting:
                if not self._running and self._thread is not None:
                    break                                   # stopping: leave the rest
                try:
                    if self._read_one(store, row):
                        read += 1
                    self._refresh_batch(store, row["batch_id"])
                except CloudError as e:
                    log.info("gauge pass stopped: %s", e)    # offline: try again next pass
                    break
            return read

    def _read_one(self, store: GaugeStore, row: dict[str, Any]) -> bool:
        image_id, batch_id = str(row["id"]), str(row["batch_id"])
        if not store.gauge_claim(image_id, _now()):
            return False                                    # another agent has it
        try:
            data = store.gauge_download(row["original_path"])
        except CloudError:
            self._release(store, image_id)
            raise
        try:
            outcome = self._reader(data, str(row.get("filename") or ""))
        except PhotoFailed as e:
            log.info("gauge photo %s failed: %s", image_id, e)
            store.gauge_save_image(image_id, {
                "status": "failed", "state": "unreadable", "error": str(e),
                "processed_at": _now()})
            return False
        except Exception as e:                              # a reader bug must not stop the batch
            log.exception("gauge photo %s: the reader broke", image_id)
            store.gauge_save_image(image_id, {
                "status": "failed", "state": "unreadable",
                "error": f"the reader failed ({type(e).__name__})", "processed_at": _now()})
            return False
        values = dict(outcome.values)
        try:
            if outcome.annotated_png:
                path = annotated_path(batch_id, image_id)
                store.gauge_upload(path, outcome.annotated_png, "image/png")
                values["annotated_path"] = path
            store.gauge_save_image(image_id, {**values, "status": "done", "processed_at": _now()})
        except CloudError:
            self._release(store, image_id)
            raise
        return True

    @staticmethod
    def _release(store: GaugeStore, image_id: str) -> None:
        """Offline mid-photo: put it back so the next pass reads it."""
        with contextlib.suppress(CloudError):
            store.gauge_save_image(image_id, {"status": "pending", "claimed_at": None})

    @staticmethod
    def _refresh_batch(store: GaugeStore, batch_id: str) -> None:
        """The batch's counters, and 'done' once nothing in it is waiting."""
        statuses = store.gauge_image_statuses(batch_id)
        waiting = sum(1 for s in statuses if s in ("pending", "processing"))
        values: dict[str, Any] = {
            "done_count": sum(1 for s in statuses if s in ("done", "failed")),
            "failed_count": sum(1 for s in statuses if s == "failed"),
            "status": "processing" if waiting else "done",
        }
        if not waiting:
            values["finished_at"] = _now()
        store.gauge_update_batch(batch_id, values)

