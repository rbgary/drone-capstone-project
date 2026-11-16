"""Processing recorded flights through the data pipeline, one at a time, in a
process of its own — the DPP switch on the Control page.

WHEN. On by default in Auto and in Manual alike (the owner, 2026-10-09; Manual
was off by default until then), and the operator can turn it off — before a
session starts or during one. The choice lasts until the session ends. It is
read when a flight BEGINS and recorded on it, so switching mid-flight never
leaves a flight half-decided. When a flight marked for
processing ends — its CSV already closed and on disk (CLAUDE.md invariant 6:
write first) — it is queued here.

WHERE. `dronedeck process --flight <id>`, the same command a developer runs,
started as a CHILD PROCESS at a lower priority. Never in the agent's own
process: the pipeline's CPU work beside the 50 Hz flight loop would compete
for Python's one interpreter lock (features/pipeline/data-pipeline.txt), and a
stage that crashes or hangs must never take the radio with it. The frozen
desktop agent is the whole CLI with freeze_support() first
(packaging/sidecar.py), so the same command works there.

AFTER THE DATA HAS ARRIVED, NEVER IN FLIGHT (2026-11-15). A flight is queued
once it has landed and its CSV is closed; nothing is judged while the drone is
in the air (story 4.9's live verdicts were removed with the camera).

WHAT COMES OUT. results/<flight id>/result.json, written by the pipeline. This
module only tracks each job — queued, running, done, failed with a reason — and
reports every change to its listener (the session: the app, and the flight's
line in the session history). Nothing here reads or judges a verdict.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

#: A flight's id goes into a path (results/<id>/) and onto a command line.
FLIGHT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: Longest a single flight may take. The stubs finish in well under a second;
#: the real stages will be slower, but a job still running after this is stuck.
TIMEOUT_S = 15 * 60
#: How many recent jobs the app is shown.
KEEP_JOBS = 20


class JobState:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


#: What a job processes: one flight, or a session around its flights.
FLIGHT, SESSION = "flight", "session"
KINDS = (FLIGHT, SESSION)


@dataclass
class Job:
    #: The flight's id — or, for a session job (kind "session"), the session's.
    flight_id: str
    state: str = JobState.QUEUED
    queued_at: str = ""
    finished_at: str | None = None
    #: Why it failed, in words; None otherwise.
    error: str | None = None
    kind: str = FLIGHT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _cli(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "dronedeck.cli", *args]


def default_command(flight_id: str) -> list[str]:
    """The pipeline command for one flight, frozen or from source."""
    return _cli("process", "--flight", flight_id)


def default_session_command(session_id: str) -> list[str]:
    """The pipeline command for a session around its flights."""
    return _cli("process", "--session", session_id)


def _lower_priority(process: subprocess.Popen[bytes]) -> None:
    """Behind the flight loop, not beside it. Best effort: a failure here
    changes how fast a result arrives, never whether."""
    with contextlib.suppress(Exception):
        if hasattr(os, "setpriority"):
            os.setpriority(os.PRIO_PROCESS, process.pid, 10)


def _last_line(text: bytes) -> str:
    lines = [line.strip() for line in text.decode("utf-8", "replace").splitlines()
             if line.strip()]
    return lines[-1][:300] if lines else ""


class ProcessingQueue:
    """Flights waiting to be processed, run one after another on one thread."""

    def __init__(self, *, on_change: Callable[[Job], None] | None = None,
                 command: Callable[[str], list[str]] = default_command,
                 session_command: Callable[[str], list[str]] = default_session_command,
                 timeout_s: float = TIMEOUT_S) -> None:
        self._on_change = on_change
        self._command = command
        self._session_command = session_command
        self._timeout_s = timeout_s
        self._jobs: dict[str, Job] = {}
        self._listeners: dict[str, Callable[[Job], None]] = {}
        self._lock = threading.Lock()
        #: Keys in the order submitted; None stops the worker.
        self._waiting: queue.Queue[str | None] = queue.Queue()
        self._running: subprocess.Popen[bytes] | None = None
        self._worker: threading.Thread | None = None
        self._closed = False

    # ── the queue ────────────────────────────────────────────────────────

    def submit(self, flight_id: str, *, on_change: Callable[[Job], None] | None = None,
               kind: str = FLIGHT) -> Job:
        """Queue a flight (or, with kind "session", a session). Queuing one
        already queued or running changes nothing; one that finished is run
        again (its result is replaced)."""
        if kind not in KINDS:
            raise ValueError(f"{kind!r} is not a kind of job")
        if not FLIGHT_ID.match(flight_id or ""):
            raise ValueError(f"{flight_id!r} is not a {kind} id")
        key = flight_id
        with self._lock:
            if self._closed:
                raise RuntimeError("the processing queue is closed")
            existing = self._jobs.get(key)
            if existing is not None and existing.state in (JobState.QUEUED, JobState.RUNNING):
                return existing
            job = Job(flight_id=flight_id, queued_at=_now(), kind=kind)
            self._jobs[key] = job
            if on_change is not None:
                self._listeners[key] = on_change
            self._trim()
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="processing",
                                                daemon=True)
                self._worker.start()
        self._waiting.put(key)
        self._changed(job)
        return job

    def job(self, flight_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(flight_id)

    def jobs(self) -> list[dict[str, Any]]:
        """Newest first."""
        with self._lock:
            return [j.to_dict() for j in reversed(self._jobs.values())]

    def close(self) -> None:
        """Stop taking work and end the flight being processed. Called when the
        agent shuts down; a flight not finished is simply processed again the
        next time someone asks."""
        with self._lock:
            self._closed = True
            running = self._running
        self._waiting.put(None)
        if running is not None:
            with contextlib.suppress(Exception):
                running.kill()

    def wait_idle(self, timeout_s: float = 10.0) -> bool:
        """For tests: True once nothing is queued or running."""
        import time
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                busy = any(j.state in (JobState.QUEUED, JobState.RUNNING)
                           for j in self._jobs.values())
            if not busy:
                return True
            time.sleep(0.01)
        return False

    # ── the worker ───────────────────────────────────────────────────────

    def _work(self) -> None:
        while True:
            key = self._waiting.get()
            if key is None:
                return
            with self._lock:
                job = self._jobs.get(key)
                if job is None or job.state != JobState.QUEUED:
                    continue
                job.state = JobState.RUNNING
            self._changed(job)
            flight_id = job.flight_id
            error = self._run(flight_id, job.kind)
            with self._lock:
                job.state = JobState.FAILED if error else JobState.DONE
                job.error = error
                job.finished_at = _now()
            if error:
                log.warning("processing %s %s failed: %s", job.kind, flight_id, error)
            else:
                log.info("processed %s %s", job.kind, flight_id)
            self._changed(job)

    def _run(self, flight_id: str, kind: str = FLIGHT) -> str | None:
        """Run the pipeline on one flight or session. None when it succeeded,
        else why not."""
        flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        argv = (self._session_command if kind == SESSION else self._command)(flight_id)
        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, creationflags=flags)
        except OSError as e:
            return f"The pipeline could not be started: {e.strerror or e}."
        with self._lock:
            self._running = process
        _lower_priority(process)
        try:
            out, err = process.communicate(timeout=self._timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return f"The pipeline took longer than {self._timeout_s / 60:.0f} minutes and " \
                   f"was stopped."
        finally:
            with self._lock:
                self._running = None
        if process.returncode == 0:
            return None
        said = _last_line(out) or _last_line(err)
        return said or f"The pipeline stopped with code {process.returncode}."

    def _changed(self, job: Job) -> None:
        with self._lock:
            listener = self._listeners.get(job.flight_id)
        for notify in (self._on_change, listener):
            if notify is None:
                continue
            try:
                notify(job)
            except Exception:
                log.exception("a processing listener failed")

    def _trim(self) -> None:
        """Keep the last KEEP_JOBS; never drop one that is still to run."""
        while len(self._jobs) > KEEP_JOBS:
            oldest = next((fid for fid, j in self._jobs.items()
                           if j.state in (JobState.DONE, JobState.FAILED)), None)
            if oldest is None:
                return
            del self._jobs[oldest]
            self._listeners.pop(oldest, None)
