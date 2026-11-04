"""Which of a session's frames go through the display chain, in what order,
and what is already done.

    <session>/upscaled/<seq>.png   each frame, made by the steps (steps.py)
    <session>/upscaled.csv         one row per frame done: seq, file, steps,
                                   done_at, ms, error
    <session>/display.json         the session's job: queued, running, done,
                                   failed — what the resume reads

WHICH FRAMES: the CAPTURED ones (frames.csv upload = 1) — what goes to the
web, at most two a second plus every manual capture (camera/recording.py).
IN WHAT ORDER: the frames a finding rests on first (result.json
evidence_frames, the flights' and the session's own), then the manual
captures, then the rest; oldest first within each. A session too long to
finish before the laptop closes still has the frames people will look at.

WHAT IS DONE is upscaled.csv, never memory: a row is written only once its
image is in place (written to a temp file, then renamed), so a chunk killed
mid-frame leaves that frame to do again and nothing half-written. A row
made with other steps (steps_key) does not count — a new version does every
frame again. A frame that failed is recorded with why and not tried again
at the same version: one corrupt file must not hold up the session forever.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from dronedeck.camera.recording import INDEX_NAME
from dronedeck.display.steps import Gray, Step, steps_key

log = logging.getLogger(__name__)

FOLDER = "upscaled"
INDEX = "upscaled.csv"
STATE = "display.json"
COLUMNS = ["seq", "file", "steps", "done_at", "ms", "error"]

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"
#: A job in one of these when the app closed was never finished.
INTERRUPTED = (QUEUED, RUNNING)


@dataclass(frozen=True)
class Chunk:
    """What one chunk did, and what is left after it."""

    done: int
    failed: int
    remaining: int

    def to_dict(self) -> dict[str, int]:
        return {"done": self.done, "failed": self.failed, "remaining": self.remaining}


def _flights(folder: Path) -> list[str]:
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(f["id"]) for f in meta.get("flights") or [] if f.get("id")]


def _evidence(result_file: Path) -> set[int]:
    try:
        result = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    seqs: set[int] = set()
    for finding in result.get("findings") or []:
        for seq in finding.get("evidence_frames") or []:
            if isinstance(seq, int):
                seqs.add(seq)
    return seqs


def evidence_frames(folder: Path, results_root: Path) -> set[int]:
    """Every frame a finding rests on: the session's flights' results and the
    session's own (results/<flight>/, results/sessions/<session>/)."""
    seqs: set[int] = set()
    for flight_id in _flights(folder):
        seqs |= _evidence(results_root / flight_id / "result.json")
    seqs |= _evidence(results_root / "sessions" / folder.name / "result.json")
    return seqs


def captured(folder: Path) -> dict[int, tuple[Path, bool]]:
    """seq → (file, captured by hand) for every frame frames.csv marks for
    upload. Read here rather than with camera/recording.py's read_index: that
    one is for sessions that have ENDED, and this runs while one may still be
    recording — a row caught half-written (the recorder appends the file, then
    its row) is skipped, and the next chunk sees it whole."""
    index = folder / INDEX_NAME
    if not index.exists():
        return {}
    out: dict[int, tuple[Path, bool]] = {}
    with index.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                seq, file, upload = int(row["seq"]), row["file"], row["upload"]
            except (KeyError, TypeError, ValueError):
                continue
            if upload == "1" and file:
                out[seq] = (folder / file, row.get("source") == "manual")
    return out


def wanted(folder: Path, results_root: Path) -> list[tuple[int, Path]]:
    """(seq, file) for every captured frame, in the order they are done."""
    frames = captured(folder)
    files = {seq: path for seq, (path, _) in frames.items()}
    manual = {seq for seq, (_, by_hand) in frames.items() if by_hand}
    evidence = evidence_frames(folder, results_root)

    def rank(seq: int) -> tuple[int, int]:
        return (0 if seq in evidence else 1 if seq in manual else 2, seq)

    return [(seq, files[seq]) for seq in sorted(files, key=rank)]


def done(folder: Path, key: str) -> set[int]:
    """Frames upscaled.csv lists as made with these steps — failed ones too."""
    index = folder / INDEX
    if not index.exists():
        return set()
    seqs: set[int] = set()
    with index.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("steps") != key:
                continue
            try:
                seqs.add(int(row["seq"]))
            except (KeyError, ValueError):
                continue
    return seqs


def _append(folder: Path, row: dict[str, Any]) -> None:
    index = folder / INDEX
    new = not index.exists()
    with index.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def _write_png(path: Path, img: Gray) -> None:
    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ValueError("the image could not be encoded")
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(buf.tobytes())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _read_gray(path: Path) -> Gray:
    img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError("not a readable image")
    return np.asarray(img, dtype=np.uint8)


def run_chunk(folder: Path, results_root: Path, steps: list[Step], *, limit: int = 0,
              clock: Callable[[], float] = time.perf_counter) -> Chunk:
    """Make up to `limit` frames not yet done (0: every one left), in order,
    and say what is left."""
    key = steps_key(steps)
    finished = done(folder, key)
    todo = [(seq, path) for seq, path in wanted(folder, results_root) if seq not in finished]
    batch = todo[:limit] if limit > 0 else todo
    out_dir = folder / FOLDER
    out_dir.mkdir(exist_ok=True)
    made = failed = 0
    for seq, path in batch:
        name = f"{seq:06d}.png"
        start = clock()
        try:
            img = _read_gray(path)
            for step in steps:
                img = step.run(img)
            _write_png(out_dir / name, img)
            error = ""
            made += 1
        except Exception as e:  # noqa: BLE001 — one frame must not stop the session
            error = f"{type(e).__name__}: {e}"[:200]
            failed += 1
            log.warning("frame %d of session %s was not made: %s", seq, folder.name, error)
        _append(folder, {"seq": seq, "file": f"{FOLDER}/{name}" if not error else "",
                         "steps": key, "done_at": datetime.now(UTC).isoformat(),
                         "ms": round((clock() - start) * 1000), "error": error})
    return Chunk(done=made, failed=failed, remaining=len(todo) - len(batch))


def set_state(folder: Path, state: str, error: str | None = None) -> None:
    """The session's job state, for the resume. A temp file, then a rename."""
    fd, temp = tempfile.mkstemp(dir=folder, prefix=".display.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"state": state, "error": error}, f)
        os.replace(temp, folder / STATE)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def interrupted(sessions_root: Path) -> list[str]:
    """Sessions whose display job the last run of the app left unfinished,
    oldest first. A FAILED one is not among them, as with processing
    (history.interrupted_flights): it failed for a reason."""
    found: list[tuple[float, str]] = []
    for state_file in sessions_root.glob(f"*/{STATE}"):
        try:
            state = json.loads(state_file.read_text(encoding="utf-8")).get("state")
        except (OSError, ValueError, AttributeError):
            continue
        if state in INTERRUPTED:
            found.append((state_file.parent.stat().st_mtime, state_file.parent.name))
    return [session_id for _, session_id in sorted(found)]


def sessions_with_frames(sessions_root: Path) -> Iterable[Path]:
    """Every session folder holding a frames index, oldest first — what
    `dronedeck upscale --all` walks."""
    folders = [p.parent for p in sessions_root.glob("*/frames.csv")]
    return sorted(folders, key=lambda p: p.stat().st_mtime)
