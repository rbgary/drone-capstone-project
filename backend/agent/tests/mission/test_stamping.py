"""The inspection point reaches every reading, the upload survives a database
that has not had the migration yet, and the guard flies a room's own fence."""

from __future__ import annotations

from dronedeck.mission.plan.geofence import Geofence
from dronedeck.safety.flight_guard import FlightGuard, GuardContext, Reason
from dronedeck.sync.cloud import SupabaseCloud, without_empty_point_id
from dronedeck.telemetry.correction import ThermalEngine
from dronedeck.telemetry.row import TelemetryRow, TempUnit, build_row
from tests.test_flight_guard import GROUND, snap


def a_row(point_id: str | None) -> TelemetryRow:
    correction = ThermalEngine(25.0, 22.0).process(25.0, 0, 1013.25)
    return build_row(index=0, recorded_at="2026-09-28T00:00:00+00:00", flight_id="f",
                     correction=correction, battery_v=4.0, thrust=0,
                     position=(0.0, 0.0, 0.3), unit=TempUnit.CELSIUS, point_id=point_id)


class TestReadings:
    def test_a_row_carries_the_point_being_held(self):
        assert a_row("P2").point_id == "P2"
        assert a_row(None).point_id is None

    def test_the_column_is_always_in_the_row_so_the_csv_header_has_it(self):
        assert "point_id" in a_row(None).to_dict()


class TestUpload:
    def test_a_batch_with_no_point_sends_no_point_key(self):
        rows = [{"index": 0, "point_id": None}, {"index": 1, "point_id": None}]
        assert all("point_id" not in r for r in without_empty_point_id(rows))

    def test_a_batch_with_a_point_keeps_the_key_on_every_row(self):
        rows = [{"index": 0, "point_id": None}, {"index": 1, "point_id": "P1"}]
        assert all("point_id" in r for r in without_empty_point_id(rows))

    def test_a_database_without_the_column_still_gets_the_readings(self, monkeypatch):
        sent: list[list[dict]] = []

        class Table:
            def upsert(self, rows, **_):
                self.rows = rows
                return self

            def execute(self):
                if any("point_id" in r for r in self.rows):
                    raise RuntimeError("Could not find the 'point_id' column of 'telemetry'")
                sent.append(self.rows)

        cloud = SupabaseCloud("https://example.supabase.co", "anon")
        monkeypatch.setattr(cloud, "_table", lambda name: Table())
        cloud.insert_telemetry([{"index": 0, "point_id": "P1"}])
        assert sent == [[{"index": 0}]]


class TestRoomFence:
    def guard(self, fence) -> FlightGuard:
        return FlightGuard(GuardContext(ground_z=GROUND, fence_half_extent_m=2.0,
                                        max_height_m=1.0, fence=fence))

    def test_outside_the_rooms_polygon_lands_even_inside_the_default_square(self):
        room = Geofence.rectangle(-1.0, -1.0, 1.0, 1.0)
        verdict = self.guard(room.contains).check(
            snap(**{"stateEstimate__x": 1.5}), now=10.0)
        assert verdict.reason is Reason.OUTSIDE_FENCE

    def test_inside_the_rooms_polygon_flies(self):
        room = Geofence.rectangle(-1.0, -1.0, 1.0, 1.0)
        assert self.guard(room.contains).check(snap(), now=10.0).ok

    def test_without_a_room_the_square_still_applies(self):
        assert self.guard(None).check(snap(**{"stateEstimate__x": 1.5}), now=10.0).ok
