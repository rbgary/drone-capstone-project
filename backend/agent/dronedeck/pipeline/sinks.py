"""Where a result goes — the pipeline's output port, and its adapter for this
laptop.

    <data folder>/results/<flight id>/result.json    the verdicts
    <data folder>/results/<flight id>/work/          the stages' own files

Running the pipeline again on the same flight REPLACES result.json (atomic:
a temporary file, then a rename) — one flight, one current result. Uploading
results to Supabase is a later adapter; this one is the record that exists
first, like every CSV (CLAUDE.md invariant 6).
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from dronedeck.pipeline.contracts import FlightResult


class ResultSink(Protocol):
    def workdir(self, flight_id: str) -> Path: ...
    def save(self, result: FlightResult) -> str: ...


def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name))
                for f in dataclasses.fields(value) if not f.name.startswith("_")}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


class LocalResultSink:
    def __init__(self, root: Path) -> None:
        self.root = root

    def workdir(self, flight_id: str) -> Path:
        path = self.root / flight_id / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, result: FlightResult) -> str:
        folder = self.root / result.flight_id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "result.json"
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".result.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(to_jsonable(result), f, indent=2)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return str(target)
