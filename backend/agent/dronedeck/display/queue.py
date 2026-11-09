"""Sessions waiting for the display chain, worked one chunk at a time.

WHEN. A session is queued when one of its flights' processing is done, and
when the session's own is (session.py, _processing_changed) — so it follows
the DPP switch: a session nobody processes is not upscaled either. A session
queued again later only does the frames it has not done (frames.py).

WHERE. `dronedeck upscale --session <id> --chunk <n>`, a CHILD PROCESS at a
lower priority, exactly as processing runs (processing.py): never beside the
50 Hz flight loop in the agent's own process.

IN CHUNKS, NEVER IN FLIGHT. A frame takes 0.6–1.1 s on the CPU and a session
can hold a thousand, so a job is many short runs of CHUNK frames. Before each
one the queue asks `paused` — the session says whether a flight is in the
air — and waits while it is. Asking between chunks, not suspending a
process, works the same on Windows, macOS and Linux; a takeoff waits for at
most one chunk, a few seconds, at a lower priority than the flight.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from dronedeck.display.frames import DONE, FAILED, QUEUED, RUNNING
from dronedeck.processing import FLIGHT_ID, _cli, _last_line, _lower_priority

log = logging.getLogger(__name__)

#: Frames per child process: ~3–6 s on the CPU, the longest a takeoff waits.
CHUNK = 5
#: A chunk still running after this is stuck.
TIMEOUT_S = 5 * 60
#: How often a paused queue looks again.
POLL_S = 1.0
KEEP_JOBS = 20


@dataclass
class DisplayJob:
    session_id: str
    state: str = QUEUED
    queued_at: str = ""
    finished_at: str | None = None
    error: str | None = None
    made: int = 0
    failed: int = 0
    remaining: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def default_command(session_id: str, chunk: int) -> list[str]:
    """The display chain for the next chunk of a session, frozen or from source."""
    return _cli("upscale", "--session", session_id, "--chunk", str(chunk))


class DisplayQueue:
    """Sessions waiting for the display chain, one after another on one thread."""

    def __init__(self, *, on_change: Callable[[DisplayJob], None] | None = None,
                 command: Callable[[str, int], list[str]] = default_command,
                 chunk: int = CHUNK, timeout_s: float = TIMEOUT_S,
                 poll_s: float = POLL_S) -> None:
        self.on_change = on_change
        #: True while the next chunk must wait — a flight in the air.
        self.paused: Callable[[], bool] = lambda: False
        self._command = command
        self._chunk = chunk
        self._timeout_s = timeout_s
        self._poll_s = poll_s
        self._jobs: dict[str, DisplayJob] = {}
        self._lock = threading.Lock()
        self._waiting: queue.Queue[str | None] = queue.Queue()
        self._running: subprocess.Popen[bytes] | None = None
        self._worker: threading.Thread | None = None
        self._closed = threading.Event()

    def submit(self, session_id: str) -> DisplayJob:
        """Queue a session. One already queued or running is left as it is;
        one that finished is run again, for the frames it has not done."""
        if not FLIGHT_ID.match(session_id or ""):
            raise ValueError(f"{session_id!r} is not a session id")
        with self._lock:
            if self._closed.is_set():
                raise RuntimeError("the display queue is closed")
            existing = self._jobs.get(session_id)
            if existing is not None and existing.state in (QUEUED, RUNNING):
                return existing
            self._jobs.pop(session_id, None)  # re-queued goes to the back
            job = DisplayJob(session_id=session_id, queued_at=_now())
            self._jobs[session_id] = job
            self._trim()
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="display", daemon=True)
                self._worker.start()
        self._waiting.put(session_id)
        self._changed(job)
        return job

    def jobs(self) -> list[dict[str, Any]]:
        """Newest first."""
        with self._lock:
            return [j.to_dict() for j in reversed(self._jobs.values())]

    def close(self) -> None:
        """Stop taking work and end the chunk running. The session's state
        stays "running" on disk, so the next start resumes it."""
        with self._lock:
            self._closed.set()
            running = self._running
        self._waiting.put(None)
        if running is not None:
            with contextlib.suppress(Exception):
                running.kill()

    def wait_idle(self, timeout_s: float = 10.0) -> bool:
        """For tests: True once nothing is queued or running."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if not any(j.state in (QUEUED, RUNNING) for j in self._jobs.values()):
                    return True
            time.sleep(0.01)
        return False

    # ── the worker ───────────────────────────────────────────────────────

    def _work(self) -> None:
        while True:
            session_id = self._waiting.get()
            if session_id is None:
                return
            with self._lock:
                job = self._jobs.get(session_id)
                if job is None or job.state != QUEUED:
                    continue
                job.state = RUNNING
            self._changed(job)
            error = self._run_job(job)
            if self._closed.is_set():
                return
            with self._lock:
                job.state = FAILED if error else DONE
                job.error = error
                job.finished_at = _now()
            if error:
                log.warning("display chain for session %s failed: %s", session_id, error)
            else:
                log.info("display chain done for session %s: %d made, %d failed",
                         session_id, job.made, job.failed)
            self._changed(job)

    def _run_job(self, job: DisplayJob) -> str | None:
        """Chunk after chunk until nothing is left. None when done, else why not."""
        while True:
            while self.paused():
                if self._closed.wait(self._poll_s):
                    return None
            if self._closed.is_set():
                return None
            said, error = self._run_chunk(job.session_id)
            if error:
                return error
            with self._lock:
                job.made += said.get("done", 0)
                job.failed += said.get("failed", 0)
                job.remaining = said.get("remaining", 0)
            if not job.remaining:
                return None
            if not said.get("done", 0) and not said.get("failed", 0):
                return "A chunk made no frames and left some: stopped rather than loop."
            self._changed(job)

    def _run_chunk(self, session_id: str) -> tuple[dict[str, int], str | None]:
        flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        try:
            process = subprocess.Popen(
                self._command(session_id, self._chunk), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=flags)
        except OSError as e:
            return {}, f"The display chain could not be started: {e.strerror or e}."
        with self._lock:
            self._running = process
        _lower_priority(process)
        try:
            out, err = process.communicate(timeout=self._timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return {}, f"A chunk took longer than {self._timeout_s / 60:.0f} minutes and " \
                       f"was stopped."
        finally:
            with self._lock:
                self._running = None
        if process.returncode != 0:
            said = _last_line(out) or _last_line(err)
            return {}, said or f"The display chain stopped with code {process.returncode}."
        try:
            report = json.loads(_last_line(out))
            return {k: int(report[k]) for k in ("done", "failed", "remaining")}, None
        except (ValueError, KeyError, TypeError):
            return {}, "The display chain did not say what it did."

    def _changed(self, job: DisplayJob) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(job)
        except Exception:
            log.exception("a display listener failed")

    def _trim(self) -> None:
        """Keep the last KEEP_JOBS; never drop one that is still to run."""
        while len(self._jobs) > KEEP_JOBS:
            oldest = next((sid for sid, j in self._jobs.items() if j.state in (DONE, FAILED)),
                          None)
            if oldest is None:
                return
            del self._jobs[oldest]
