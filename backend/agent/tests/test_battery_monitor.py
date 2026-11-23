"""Reporting a battery under 30 % (sprint 3, task 19)."""

from __future__ import annotations

from types import MappingProxyType

from dronedeck.flight.preflight import VBAT_EMPTY, VBAT_FULL
from dronedeck.safety.battery_monitor import (
    CLEAR_PERCENT,
    FLIGHT_CLEAR_V,
    FLIGHT_LOW_V,
    LOW_BATTERY_PERCENT,
    SUSTAIN_S,
    BatteryMonitor,
    motors_running,
)
from dronedeck.telemetry.stream import Snapshot
from tests.test_session import rig, start_and_confirm  # noqa: F401  (rig is a fixture)


def volts(percent: float) -> float:
    return VBAT_EMPTY + (VBAT_FULL - VBAT_EMPTY) * percent / 100


class TestMonitor:
    def test_a_full_battery_is_not_low(self):
        s = BatteryMonitor().update(volts(90), 0.0)
        assert (s.low, s.just_went_low) == (False, False)
        assert s.percent == 90

    def test_under_30_for_long_enough_is_low_and_said_once(self):
        m = BatteryMonitor()
        assert not m.update(volts(25), 0.0).low                    # just crossed
        went = m.update(volts(25), SUSTAIN_S)
        assert went.low and went.just_went_low
        again = m.update(volts(24), SUSTAIN_S + 1)
        assert again.low and not again.just_went_low               # reported once
        assert LOW_BATTERY_PERCENT == 30

    def test_a_brief_sag_under_thrust_is_not_low(self):
        m = BatteryMonitor()
        m.update(volts(20), 0.0)                                   # throttle step
        assert not m.update(volts(60), 0.5).low
        assert not m.update(volts(20), SUSTAIN_S).low              # the clock restarted

    def test_it_clears_only_above_the_hysteresis_band(self):
        m = BatteryMonitor(sustain_s=0)
        assert m.update(volts(25), 0.0).low
        assert m.update(volts(32), 1.0).low                        # 30-35: still low
        assert not m.update(volts(CLEAR_PERCENT + 5), 2.0).low     # a fresh battery

    def test_no_reading_changes_nothing(self):
        s = BatteryMonitor().update(None, 0.0)
        assert s.percent is None and not s.low


class TestUnderLoad:
    """2026-11-07: a 64-74 % battery hovered at 3.40-3.50 V, which the ground
    map calls 11-22 % — the old monitor said "low" 4 s after takeoff."""

    def test_a_healthy_battery_sagging_in_flight_is_not_low(self):
        m = BatteryMonitor()
        assert m.update(3.87, 0.0).percent == 63               # on the ground
        for t in range(1, 30):
            s = m.update(3.45, float(t), motors_on=True)       # hovering
        assert not s.low
        assert s.percent == 63                                  # held from the ground

    def test_sustained_under_the_flight_line_is_low(self):
        m = BatteryMonitor()
        m.update(3.95, 0.0)
        assert not m.update(FLIGHT_LOW_V - 0.05, 1.0, motors_on=True).low
        went = m.update(FLIGHT_LOW_V - 0.05, 1.0 + SUSTAIN_S, motors_on=True)
        assert went.low and went.just_went_low and went.motors_on
        assert "under load" in went.message()

    def test_a_count_does_not_carry_across_takeoff(self):
        m = BatteryMonitor()
        m.update(volts(25), 0.0)                                # under 30 % on the ground
        m.update(3.45, 2.0, motors_on=True)                     # takes off: a new count
        assert not m.update(volts(25), 3.5).low                 # lands: count restarts
        assert m.update(volts(25), 3.5 + SUSTAIN_S).low

    def test_it_clears_in_flight_only_above_the_flight_clear_line(self):
        m = BatteryMonitor(sustain_s=0)
        assert m.update(3.35, 0.0, motors_on=True).low
        assert m.update(FLIGHT_CLEAR_V - 0.01, 1.0, motors_on=True).low
        assert not m.update(FLIGHT_CLEAR_V + 0.01, 2.0, motors_on=True).low

    def test_motors_running_reads_any_motor(self):
        def snap(**v):
            return Snapshot(MappingProxyType(v), 0.0)
        assert not motors_running(snap(**{"pm.vbat": 3.9}))
        assert motors_running(snap(**{"motor.m3": 30000}))


class TestSessionReportsIt:
    def test_telemetry_carries_the_battery_and_low_is_announced(self, rig):  # noqa: F811
        start_and_confirm(rig)
        rig.session._battery = BatteryMonitor(sustain_s=0)
        rig.session._publish_telemetry(Snapshot(MappingProxyType({"pm.vbat": volts(80)}), 1.0))
        rig.session._publish_telemetry(Snapshot(MappingProxyType({"pm.vbat": volts(20)}), 2.0))
        rig.session._publish_telemetry(Snapshot(MappingProxyType({"pm.vbat": volts(19)}), 3.0))

        telemetry = [p for k, p in rig.events if k == "telemetry"]
        assert telemetry[0]["battery"]["low"] is False
        assert telemetry[-1]["battery"]["low"] is True
        assert telemetry[-1]["battery"]["threshold_percent"] == 30

        lows = [p for k, p in rig.events if k == "battery_low"]
        assert len(lows) == 1
        assert "30%" in lows[0]["message"]
