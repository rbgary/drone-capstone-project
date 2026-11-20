"""Report when the battery falls below 30 % (sprint 3, task 19).

Requirement: "threshold should be <30 and drone should report when it hits
threshold". This module REPORTS only — it changes nothing about flying:

  * The drone's own verdicts still decide. The firmware's pm.state and
    sys.canfly, and the guard's 3.10 V floor (flight_guard._battery_verdict),
    are what end a flight. Invariant 1 in CLAUDE.md: no invented voltage gate.
  * Returning to base on low battery is sprint 4 (task 8, Yordanos; task 5,
    Hannah). That work can act on `BatteryStatus.low`.

THE PERCENTAGE is the same linear estimate the checks show
(checks.battery_percent: 3.30 V = 0 %, 4.20 V = 100 %). It is only read with
the MOTORS OFF. A LiPo's voltage sags under thrust — on 2026-11-07 a battery
at 64-74 % on the ground hovered at 3.40-3.50 V, which the linear map calls
11-22 %, so a ground threshold applied in flight said "low" 4 s after every
takeoff. So there are two thresholds:

  * MOTORS OFF  low under LOW_BATTERY_PERCENT (30 % = 3.57 V), clear above
                CLEAR_PERCENT (35 %).
  * MOTORS ON   low under FLIGHT_LOW_V (3.40 V), clear above FLIGHT_CLEAR_V.
                In flight `percent` is the last motors-off reading, held —
                the loaded voltage is not a charge.

Either way LOW only after it has stayed under for SUSTAIN_S, so a throttle step
that sags the cell for a moment is not low; clearing above a higher line stops
a reading at the threshold from flickering the warning on and off.

FLIGHT_LOW_V IS A FIRST ESTIMATE from three lab flights on 2026-11-07: two
healthy batteries never sustained under it (lowest 3.42 V and 3.50 V); the
flight that did (3.31-3.40 V at full thrust) fell out of the air for want of
power. Confirm it against more flights (flight-log.txt).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dronedeck.flight.checks import battery_percent

#: Below this percentage, motors off, the battery is reported low (the sprint's "<30").
LOW_BATTERY_PERCENT = 30
#: It is reported recovered only above this (hysteresis).
CLEAR_PERCENT = 35
#: Under load: below this voltage the battery is reported low.
FLIGHT_LOW_V = 3.40
#: Under load: it is reported recovered only above this.
FLIGHT_CLEAR_V = 3.45
#: How long it must stay under the threshold before it counts, seconds.
SUSTAIN_S = 3.0


@dataclass(frozen=True)
class BatteryStatus:
    percent: int | None
    vbat: float | None
    low: bool
    #: True on the ONE sample where it became low — the moment to report.
    just_went_low: bool = False
    #: Whether this was judged under load (the flight threshold).
    motors_on: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"percent": self.percent, "vbat": self.vbat, "low": self.low,
                "threshold_percent": LOW_BATTERY_PERCENT,
                "flight_threshold_v": FLIGHT_LOW_V, "motors_on": self.motors_on}

    def message(self) -> str:
        if self.motors_on:
            return (f"Battery low: {self.vbat:.2f} V under load, under {FLIGHT_LOW_V:.2f} V. "
                    f"Finish up and land soon.")
        return (f"Battery low: about {self.percent}% ({self.vbat:.2f} V), under "
                f"{LOW_BATTERY_PERCENT}%. Finish up and land soon.")


def motors_running(snap: Any) -> bool:
    """Any motor turning in a telemetry snapshot."""
    return any((snap.get(f"motor.m{i}") or 0) > 0 for i in range(1, 5))


class BatteryMonitor:
    """Feed it every telemetry snapshot; it says when the battery goes low."""

    def __init__(self, low_percent: int = LOW_BATTERY_PERCENT,
                 clear_percent: int = CLEAR_PERCENT, sustain_s: float = SUSTAIN_S,
                 flight_low_v: float = FLIGHT_LOW_V,
                 flight_clear_v: float = FLIGHT_CLEAR_V) -> None:
        self.low_percent = low_percent
        self.clear_percent = clear_percent
        self.sustain_s = sustain_s
        self.flight_low_v = flight_low_v
        self.flight_clear_v = flight_clear_v
        self.reset()

    def reset(self) -> None:
        """A new link or a new battery: forget what the last one did."""
        self._low = False
        self._under_since: float | None = None
        self._rest_percent: int | None = None
        self._motors_on = False

    def update(self, vbat: float | None, now: float, motors_on: bool = False) -> BatteryStatus:
        if vbat is None:
            return BatteryStatus(self._rest_percent, None, self._low, motors_on=motors_on)
        if motors_on != self._motors_on:
            # Takeoff or landing: the voltage jumps with the load, so a count
            # started under the other threshold does not carry over.
            self._motors_on = motors_on
            self._under_since = None
        if not motors_on:
            self._rest_percent = battery_percent(vbat)
            under = self._rest_percent < self.low_percent
            recovered = self._rest_percent > self.clear_percent
        else:
            under = vbat < self.flight_low_v
            recovered = vbat > self.flight_clear_v

        went_low = False
        if self._low:
            if recovered:
                self._low = False
                self._under_since = None
        elif under:
            if self._under_since is None:
                self._under_since = now
            if now - self._under_since >= self.sustain_s:
                self._low = True
                went_low = True
        else:
            self._under_since = None
        return BatteryStatus(self._rest_percent, round(vbat, 2), self._low, went_low, motors_on)
