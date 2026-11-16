"""Where a flight comes from — the pipeline's input port, and its adapter for
this laptop.

LocalFlightSource reads what the agent already wrote, and nothing else:

    flights/<date>/flight_<first 8 of the id>_<time>.csv   the readings
    sessions/<session id>/meta.json                        which session flew it
    sessions/<session id>/missions/<flight id>.json         the plan flown, if any

THE WHOLE FLIGHT, TAGGED BY POINT (contract v2). Rows carry point_id while the
mission controller was holding there (story 3.5); every Reading keeps it. The
stages get the whole flight as one PointData ("flight") — transit included,
since a block can start in transit — and `points` gives the per-point view:
exactly the rows stamped with each point. A flight
with no mission has no plan, and its one point is the whole flight.

A later cloud worker gets its own adapter (Supabase Storage in, the same
LoadedFlight out) — the stages never know the difference.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from dronedeck.pipeline.contracts import (
    WHOLE_FLIGHT,
    WHOLE_SESSION,
    InspectionPoint,
    PointData,
    Reading,
)

log = logging.getLogger(__name__)

#: CSV columns that are words, not measurements.
TEXT_COLUMNS = frozenset({"index", "recorded_at", "flight_id", "mode", "thermal_state",
                          "event", "temp_unit", "point_id"})
#: The words a stage is given (Reading.text); the rest are identity or units,
#: carried elsewhere.
STAGE_TEXT = ("mode", "thermal_state", "event")


class FlightNotFound(LookupError):
    """No readings for that flight on this computer."""


@dataclass(frozen=True)
class LoadedFlight:
    flight_id: str
    session_id: str | None
    temp_unit: Literal["C", "F"]
    ground_z_m: float | None
    started_at: datetime
    #: Every reading of the flight, as one point ("flight").
    whole: PointData
    #: The mission's inspection points, in order; () when none was flown.
    plan: tuple[InspectionPoint, ...] = ()

    @property
    def points(self) -> tuple[PointData, ...]:
        """One PointData per inspection point — exactly the readings stamped
        with it. A flight with no mission is its own single point."""
        if not self.plan:
            return (self.whole,)
        return tuple(
            PointData(p, tuple(r for r in self.whole.readings if r.point_id == p.id))
            for p in self.plan)

    @property
    def unassigned_readings(self) -> int:
        """Readings that belonged to no inspection point (transit, takeoff)."""
        if not self.plan:
            return 0
        return sum(1 for r in self.whole.readings if r.point_id is None)


class FlightSource(Protocol):
    def load(self, flight_id: str) -> LoadedFlight: ...


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _number(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


class LocalFlightSource:
    """A flight recorded by the agent on this laptop."""

    def __init__(self, data_root: Path) -> None:
        self.root = data_root

    def load(self, flight_id: str) -> LoadedFlight:
        csv_path = self._find_csv(flight_id)
        rows = list(self._rows(csv_path))
        if not rows:
            raise FlightNotFound(f"Flight {flight_id} has no readings in {csv_path.name}.")
        started = _time(rows[0]["recorded_at"])
        unit = rows[0].get("temp_unit") or "C"
        if unit not in ("C", "F"):
            raise FlightNotFound(f"Flight {flight_id} records temperatures in {unit!r}, "
                                 f"which is not C or F.")

        readings = tuple(self._reading(row, started) for row in rows)
        session = self._find_session(flight_id)
        plan = (self._plan(session, flight_id) if session else None) or ()

        whole = PointData(InspectionPoint(WHOLE_FLIGHT, None, 0.0, 0.0, 0.0), readings)
        return LoadedFlight(
            flight_id=flight_id, session_id=session.name if session else None,
            temp_unit="F" if unit == "F" else "C", ground_z_m=None, started_at=started,
            whole=whole, plan=plan,
        )

    # ── finding things ───────────────────────────────────────────────────

    def _find_csv(self, flight_id: str) -> Path:
        for path in sorted((self.root / "flights").glob(f"*/flight_{flight_id[:8]}_*.csv")):
            # Eight characters can collide; the rows name the whole id.
            with path.open(newline="", encoding="utf-8") as f:
                first = next(csv.DictReader(f), None)
            if first is not None and first.get("flight_id") == flight_id:
                return path
        raise FlightNotFound(f"No readings for flight {flight_id} on this computer.")

    def _find_session(self, flight_id: str) -> Path | None:
        for meta_path in (self.root / "sessions").glob("*/meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if any(f.get("id") == flight_id for f in meta.get("flights") or []):
                return meta_path.parent
        log.info("flight %s belongs to no session folder here; no plan", flight_id)
        return None

    @staticmethod
    def _rows(path: Path) -> Iterator[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as f:
            yield from csv.DictReader(f)

    @staticmethod
    def _reading(row: dict[str, str], started: datetime) -> Reading:
        at = _time(row["recorded_at"])
        return Reading(
            index=int(float(row["index"])), recorded_at=at,
            t_s=(at - started).total_seconds(),
            values={k: _number(v) for k, v in row.items() if k not in TEXT_COLUMNS},
            point_id=row.get("point_id") or None,
            text={k: row[k] for k in STAGE_TEXT if row.get(k)},
        )

    @staticmethod
    def _plan(session: Path, flight_id: str) -> tuple[InspectionPoint, ...] | None:
        """The inspection points of the mission this flight flew, in order."""
        path = session / "missions" / f"{flight_id}.json"
        if not path.exists():
            return None
        try:
            data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            return tuple(
                InspectionPoint(str(p["id"]), p.get("label"), float(p["x_m"]),
                                float(p["y_m"]), float(p["z_m"]))
                for p in data["mission"]["points"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log.warning("the plan flown by %s could not be read (%s); treating the "
                        "flight as one point", flight_id, e)
            return None


# ── a whole session (story 4.5, extended 2026-10-09) ─────────────────────
#
# THE OWNER'S ASK: "as long as a session is started we process whatever data
# comes, not only data in flight" — in Auto and in Manual. A session records
# its own samples once a second from Start session to End session
# (samples.csv, the same file the web's session vitals come from). The flights
# have results of their own at ten times the rate; the session's run covers
# everything around them.
#
#   sessions/<id>/samples.csv     one row a second: the stream variables
#   sessions/<id>/meta.json       its flights' start and end

#: A reading this close to a flight's start or end belongs to it (clock skew
#: between the sample writer and the flight recorder).
FLIGHT_EDGE_S = 1.0
#: Motors at or above this thrust are flying whatever the history says — the
#: armed idle of Manual is below it.
FLYING_THRUST = 20000.0


class SessionNotFound(LookupError):
    """No samples for that session on this computer."""


@dataclass(frozen=True)
class LoadedSession:
    session_id: str
    started_at: datetime
    #: Every sample of the session, as one point ("session"). Each reading's
    #: text["phase"] says "ground" or "flying".
    whole: PointData
    #: (start, end) of each flight flown, from the session's history.
    flights: tuple[tuple[datetime, datetime | None], ...] = ()


def session_values(cells: dict[str, str]) -> dict[str, float | None]:
    """A samples.csv row's numbers under the flight CSV's names, so the stages
    read a session exactly as they read a flight (sync/samples.py COLUMNS)."""
    from dronedeck.sync.samples import COLUMNS

    values: dict[str, float | None] = {
        column: _number(cells.get(stream)) for column, stream in COLUMNS.items()}
    values["lighthouse_received"] = _number(cells.get("lighthouse.bsReceive"))
    values["height_m"] = _number(cells.get("height_m"))
    return values


class LocalSessionSource:
    """A session recorded by the agent on this laptop."""

    def __init__(self, data_root: Path) -> None:
        self.root = data_root

    def load(self, session_id: str) -> LoadedSession:
        folder = self.root / "sessions" / session_id
        samples = folder / "samples.csv"
        if not samples.exists():
            raise SessionNotFound(f"No samples for session {session_id} on this computer.")
        rows = [r for r in LocalFlightSource._rows(samples) if r.get("recorded_at")]
        if not rows:
            raise SessionNotFound(f"Session {session_id} recorded no samples.")
        flights = self._flights(folder)
        started = _time(rows[0]["recorded_at"])
        readings = []
        for seq, row in enumerate(rows, start=1):
            at = _time(row["recorded_at"])
            values = session_values(row)
            readings.append(Reading(
                index=seq, recorded_at=at, t_s=(at - started).total_seconds(),
                values=values, point_id=None,
                text={"mode": row.get("mode") or "",
                      "phase": "flying" if self._flying(at, values, flights) else "ground"}))
        whole = PointData(InspectionPoint(WHOLE_SESSION, "the session", 0.0, 0.0, 0.0),
                          tuple(readings))
        return LoadedSession(session_id, started, whole, flights)

    @staticmethod
    def _flights(folder: Path) -> tuple[tuple[datetime, datetime | None], ...]:
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()
        out = []
        for f in meta.get("flights") or []:
            try:
                start = _time(str(f["started_at"]))
            except (KeyError, ValueError):
                continue
            end = f.get("ended_at")
            try:
                out.append((start, _time(str(end)) if end else None))
            except ValueError:
                out.append((start, None))
        return tuple(out)

    @staticmethod
    def _flying(at: datetime, values: dict[str, float | None],
                flights: tuple[tuple[datetime, datetime | None], ...]) -> bool:
        thrust = values.get("thrust")
        if thrust is not None and thrust >= FLYING_THRUST:
            return True
        for start, end in flights:
            if end is None:
                continue        # never closed (the app quit): the thrust decides
            if (start - at).total_seconds() <= FLIGHT_EDGE_S \
                    and (at - end).total_seconds() <= FLIGHT_EDGE_S:
                return True
        return False
