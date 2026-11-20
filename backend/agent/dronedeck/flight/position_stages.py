# This file was modified on 11/4/2026 with the assistance of Claude Code in VS Code
# (Claude Opus 5.5, Anthropic, 2026). A decoded station whose place is not stored is
# reported as the blocker only when too few stations are usable, matching the agent's
# positioning readiness so the stage note never contradicts the Continue button; the
# "not settled" note shows the uncertainty rounded up to 0.1 cm.

"""Where the chain from base-station light to a position breaks — the FIRST
stage that is not done, in words, with its fix.

One rule for every screen that asks "why is the drone not placed?" — the
Manual map, the stage list beside it, Auto's ② Position — so they never
disagree (2026-10-24, Samuel: "make sure the two base stations are properly
detected"). In order, because each needs the one before it:

  deck       the drone detected its positioning deck at power-on
  firmware   the deck's own firmware runs (its memory says no update is due)
  light      light reaches the deck's four sensors
  decoded    a station's sweeps are decoded into angles (bsReceive)
  stored     every decoded station's place is stored (bsGeoVal)
  settled    the position is tight enough to fly on

Pure: the facts in, the first blocker out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dronedeck.safety.flight_guard import (
    MAX_READY_VARIANCE_M2,
    PositioningStatus,
    required_stations,
    uncertainty_cm,
)


@dataclass(frozen=True)
class Blocker:
    stage: str
    text: str
    fix: str

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "text": self.text, "fix": self.fix}


def channel(station: int) -> str:
    return f"Ch {station + 1}"


def first_blocker(status: PositioningStatus, *, deck_detected: bool | None,
                  deck_needs_fw: bool | None) -> Blocker | None:
    """The first stage that is not done, or None when the drone is placed."""
    if deck_detected is False:
        return Blocker(
            "deck", "The drone does not detect its positioning deck.",
            "Unplug the battery for 10 seconds and plug it back in — decks are detected "
            "when the drone starts. If it stays missing, reseat the positioning deck.")
    if deck_needs_fw:
        return Blocker(
            "firmware", "The positioning deck's firmware is not running — the drone "
            "says it needs writing.",
            "Quit DroneDeck and run \"Reinstall the positioning deck\" from Set up, "
            "step 2, in Terminal.")
    if not status.received:
        if status.light_sensors == 0:
            return Blocker(
                "light", "No light reaches the positioning deck (0 of 4 sensors).",
                "Check both base stations' front LEDs are solid green and can see the top "
                "of the drone. Still 0 with the drone 1 m in front of a station: switch "
                "the drone off and on; then reseat the positioning deck.")
        lit = f" ({status.light_sensors} of 4 sensors)" if status.light_sensors else ""
        return Blocker(
            "decoded", f"Light reaches the deck{lit}, but no station's sweep is decoded.",
            "Give each base station its own channel — Ch 1 and Ch 2 (Set up, step 3).")
    # Only a blocker while too few stations are usable — the same rule as
    # PositioningStatus.problems() (2026-11-04). With enough usable, a second
    # station merely in view does not stop the drone being placed.
    unmeasured = [s for s in status.received if s not in status.with_geometry]
    if unmeasured and len(status.usable) < required_stations():
        names = " and ".join(channel(s) for s in unmeasured)
        return Blocker(
            "stored", f"{names} is decoded, but its place is not stored.",
            "Measure the base stations: the drone flat on the floor in view of both, "
            "then Measure.")
    variances = [v for v in status.variance_m2 if v is not None]
    if len(variances) < 3 or max(variances) > MAX_READY_VARIANCE_M2:
        worst = f" (about {uncertainty_cm(max(variances)):.1f} cm)" if variances else ""
        return Blocker(
            "settled", f"The position has not settled yet{worst}.",
            "Keep the drone still in view of the stations for a few seconds.")
    return None
