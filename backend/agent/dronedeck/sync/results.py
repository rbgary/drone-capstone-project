"""A flight's pipeline result, uploaded (supabase migration 20261009000016).

`dronedeck process` writes results/<flight>/result.json — on the laptop first
(CLAUDE.md invariant 6) — and queues an outbox record (Kind.RESULTS). The
syncer turns that file into the two tables' rows:

    pipeline_results    one row per flight: the whole result as written
    pipeline_findings   one row per finding, for the pages and notifications

THE IMAGE COLUMNS, UNTIL THEY ARE RETIRED. Results carry no frames since
pipeline "4" (contract v3, no camera), but the hosted pipeline_findings still
has evidence_frames, image_support (NOT NULL, checked) and image_note
(migration 20261009000016). Every finding is sent with their neutral values —
no frames, "cannot_tell", no note — so an upload is never refused, until a
migration drops them and this goes with it.

Re-processing a flight re-queues it: the result row is replaced (upsert on
flight_id), every finding upserted on its id (a stretch keeps its id), and the
findings the new result no longer makes are deleted. This reads the JSON the
pipeline wrote and nothing else — sync never imports the pipeline.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

RESULT_NAME = "result.json"
WORK_NAME = "work"
RANK = {"info": 0, "warning": 1, "critical": 2}


class ResultUnreadable(ValueError):
    """result.json is missing or not a pipeline result."""


def load(folder: Path) -> dict[str, Any]:
    try:
        data: dict[str, Any] = json.loads((folder / RESULT_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ResultUnreadable(f"no {RESULT_NAME} in {folder}") from e
    except json.JSONDecodeError as e:
        raise ResultUnreadable(f"{folder / RESULT_NAME} is not JSON: {e}") from e
    if "flight_id" not in data or "points" not in data:
        raise ResultUnreadable(f"{folder / RESULT_NAME} is not a pipeline result")
    return data


def result_row(result: Mapping[str, Any]) -> dict[str, Any]:
    """public.pipeline_results ← result.json."""
    findings = result.get("findings") or []
    worst = max((f["severity"] for f in findings), key=lambda s: RANK.get(s, -1),
                default=None)
    return {
        "flight_id": result["flight_id"],
        "session_id": result.get("session_id"),
        "pipeline_version": str(result.get("pipeline_version", "")),
        "stages": result.get("stages") or {},
        "created_at": result["created_at"],
        "temp_unit": result.get("temp_unit") or "C",
        "points": result.get("points") or [],
        "findings_count": len(findings),
        "worst_severity": worst,
        # Compact: [index, column, kind, reason] — a flight can carry thousands.
        "flags": [[f["index"], f.get("column"), f["kind"], f["reason"]]
                  for f in result.get("flags") or []],
        "tracks": result.get("tracks") or [],
        "segments": result.get("segments") or [],
        "frames": result.get("frames") or [],
        "failures": result.get("failures") or [],
        "summary": result.get("summary") or {},
    }


FINDING_COLUMNS = ("id", "signal", "severity", "title", "sentence", "start_index",
                   "end_index", "t_start_s", "t_end_s", "unit", "observed", "expected",
                   "delta", "z", "point_ids", "x_m", "y_m", "z_m")
#: The image columns' neutral values (see the module docstring);
#: evidence_frames is sent as an empty list beside them.
RETIRED_IMAGE_COLUMNS = {"image_support": "cannot_tell", "image_note": ""}


def is_session(result: Mapping[str, Any]) -> bool:
    """A session's own result (scope "session"): flight_id holds the session's id."""
    return result.get("scope") == "session"


def session_result_row(result: Mapping[str, Any]) -> dict[str, Any]:
    """public.pipeline_session_results (migration 20261009000018) ← a session's
    result.json — a flight's row with the session in place of the flight."""
    row = result_row(result)
    del row["flight_id"]
    row["session_id"] = result.get("session_id") or result["flight_id"]
    return row


def finding_rows(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """public.pipeline_findings ← result.json's findings. A session's findings
    belong to the session (scope "session") and to no flight."""
    session = is_session(result)
    return [{**{c: f.get(c) for c in FINDING_COLUMNS}, **RETIRED_IMAGE_COLUMNS,
             "evidence_frames": [], "point_ids": list(f.get("point_ids") or []),
             "flight_id": None if session else result["flight_id"],
             "session_id": (result.get("session_id") or result["flight_id"]) if session
             else result.get("session_id"),
             "scope": "session" if session else "flight",
             "pipeline_version": str(result.get("pipeline_version", ""))}
            for f in result.get("findings") or []]
