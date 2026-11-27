"""The SYSTEM STATUS gauge, in greyscale sensor frames (was systemStatusReader.py).

Same method as the pressure gauge (dial.py: dial cut-out, arrow-shaped needle,
needle extended to the tick ring), but the dial is a circle with its bottom cut
off, so the bezel is only checked over the upper arc. The scale is 0-100: 0-30
inclusive is ok, 30-70 exclusive is a warning, 70-100 inclusive is danger. A
needle off the scale is unreadable (drawn purple).

The status gauge sits directly below the pressure gauge, so when the pressure
gauge is in view this one is looked for in the place below it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from dronedeck.pipeline.gauge import dial
from dronedeck.pipeline.gauge.dial import Circle, Point, needle_value, weakest_sector
from dronedeck.pipeline.gauge.reticle import remove_reticle

Gray = NDArray[Any]

OK_MAX = 30.0                          # inclusive
DANGER_MIN = 70.0                      # inclusive
FULL_SCALE = 100.0
#: Of 12 wedges (0 = left, clockwise): the dial's intact upper arc.
TOP_SECTORS = range(0, 6)
#: The wedges straight down, where the dial is cut off.
BOTTOM_SECTORS = range(8, 10)
MIN_TOP_CONTRAST = 35                  # the arc must be clearly bezelled...
#: ...a bezelled dial must show where the status gauge should be...
MIN_BELOW_CONTRAST = 15
#: ...and the bottom must not be, or it is the full pressure dial.
MAX_BOTTOM_CONTRAST = 25
#: Status dial centre below the pressure dial's, in pressure radii.
BELOW_PRESSURE = 2.45
#: Status dial radius over the pressure dial's.
RADIUS_RATIO = 0.85
#: How far a detected circle may sit from that guess, in radii.
SEARCH_TOLERANCE = 0.35
#: Where the flat bottom edge can be, in radii below the centre.
CUT_SEARCH = (0.3, 0.9)
#: Columns to find it in, in radii either side: clear of the needle.
CUT_SIDES = (0.4, 0.65)
#: The needle's pivot sits this far below the dial centre, in radii.
HUB_DROP = 0.18
#: Needle darkness vs the face, tried in turn for faint needles.
DARK_RATIOS = (0.6, 0.75, 0.85)


@dataclass
class StatusReading:
    value: float | None                # None when there is no valid reading (see error)
    error: str | None = None           # why there is no reading
    raw: float | None = None           # the needle's reading before the range check
    circle: Circle | None = None       # cx, cy, r in the source image
    tip: Point | None = None           # needle tip in the source image
    tail: Point | None = None          # needle stem end (the hub, for the colour reader)

    @property
    def danger(self) -> bool | None:
        return None if self.value is None else self.value >= DANGER_MIN

    @property
    def warning(self) -> bool | None:
        return None if self.value is None else OK_MAX < self.value < DANGER_MIN

    @property
    def state(self) -> str:
        """What the web stores: unreadable | danger | warning | ok."""
        if self.error or self.value is None:
            return "unreadable"
        if self.danger:
            return "danger"
        return "warning" if self.warning else "ok"

    @property
    def label(self) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        return {"danger": "DANGER", "warning": "WARNING"}.get(self.state, "ok")

    @property
    def off_scale(self) -> bool:
        """The needle was found, but outside 0-100."""
        return self.raw is not None and self.value is None

    @property
    def summary(self) -> str:
        return self.label if self.value is None else f"{self.value:.0f}  {self.label}"


def status_reading(circle: Circle, tip: Point, tail: Point) -> StatusReading:
    """The needle's tip and tail, as a reading on the 0-100 scale."""
    value, raw, error = needle_value(circle, tip, tail, FULL_SCALE)
    if raw is None:
        return StatusReading(None, error, circle=circle)
    return StatusReading(value, error, raw, circle, tip, tail)


def status_circles(gray: Gray) -> list[Circle]:
    """Circles with a bright face and dark bezel over the top arc but not along the bottom.
    The pressure gauge is bezelled all round, so it is skipped."""
    h = gray.shape[0]
    blur = cv2.medianBlur(gray, 5)
    found = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1.2, minDist=h // 8,
                             param1=100, param2=22,
                             minRadius=max(15, h // 30), maxRadius=int(h * 0.6))
    circles = []
    for cx, cy, r in ([] if found is None else found[0][:60]):
        top = weakest_sector(gray, float(cx), float(cy), float(r), shown=TOP_SECTORS)
        bottom = weakest_sector(gray, float(cx), float(cy), float(r), shown=BOTTOM_SECTORS)
        if top > MIN_TOP_CONTRAST and bottom < MAX_BOTTOM_CONTRAST:
            circles.append((top * r, (float(cx), float(cy), float(r))))
    return [c for _, c in sorted(circles, reverse=True)]


def below(circle: Circle) -> Circle:
    """Where the status gauge sits if `circle` is the pressure gauge above it."""
    cx, cy, r = circle
    return cx, cy + BELOW_PRESSURE * r, RADIUS_RATIO * r


def pressure_dial(gray: Gray) -> Circle | None:
    """The pressure gauge above the status gauge, if the picture has both: the gauge
    dial.find_dial finds, with a status-shaped dial in the place below it."""
    circle = dial.find_dial(gray)
    if circle is None:
        return None
    return circle if weakest_sector(gray, *below(circle), shown=TOP_SECTORS) > MIN_BELOW_CONTRAST \
        else None


def find_dial(gray: Gray) -> Circle | None:
    """The status gauge. When the pressure gauge is in view, the status gauge is the one
    below it; otherwise it is the best status-shaped circle in the picture."""
    circles = status_circles(gray)
    pressure = pressure_dial(gray)
    if pressure is None:
        return circles[0] if circles else None
    guess = below(pressure)
    for cx, cy, r in circles:
        if (np.hypot(cx - guess[0], cy - guess[1]) < SEARCH_TOLERANCE * guess[2]
                and abs(r - guess[2]) < SEARCH_TOLERANCE * guess[2]):
            return cx, cy, r
    return guess


def cut_depth(gray: Gray, circle: Circle) -> float:
    """How far below the dial centre the flat bottom edge sits, as a fraction of the radius:
    the steepest drop from bright face to dark panel in the columns either side of the needle."""
    cx, cy, r = circle
    size = dial.DIAL_PX
    s = size / (2 * r)
    matrix = np.array([[s, 0, size / 2 - cx * s], [0, s, size / 2 - cy * s]])
    warped = cv2.warpAffine(gray, matrix, (size, size),
                            flags=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC,
                            borderMode=cv2.BORDER_REPLICATE)
    blurred = cv2.GaussianBlur(warped, (0, 0), 2).astype(np.float32)
    c = size / 2
    cols = np.r_[int(c - CUT_SIDES[1] * c):int(c - CUT_SIDES[0] * c),
                 int(c + CUT_SIDES[0] * c):int(c + CUT_SIDES[1] * c)]
    rows = blurred[:, cols].mean(axis=1)
    lo, hi = int(c + CUT_SEARCH[0] * c), min(size - 4, int(c + CUT_SEARCH[1] * c))
    drop = [rows[y + 3] - rows[y - 3] for y in range(lo, hi)]
    return float((lo + int(np.argmin(drop)) - c) / c)


def read_needle(gray: Gray, circle: Circle) -> tuple[Point, Point] | None:
    """(tip, tail) of the needle, trying fainter needles in turn."""
    lowest = cut_depth(gray, circle)
    for ratio in DARK_RATIOS:
        needle = dial.read_needle(gray, circle, lowest, ratio)
        if needle is not None:
            _, tip, tail = needle
            cx, cy, r = circle
            hub = np.array([cx, cy + HUB_DROP * r])
            # The head reaches far past the pivot and the stem barely does, so the end
            # farther from the pivot is the tip, whatever the arrow's shape says.
            if np.linalg.norm(np.array(tip) - hub) < np.linalg.norm(np.array(tail) - hub):
                tip, tail = tail, tip
            return tip, tail
    return None


def read_gauge(gray: Gray) -> StatusReading:
    """A sensor frame (greyscale, with the viewer's crosshair), read end to end."""
    gray = remove_reticle(gray)
    circle = find_dial(gray)
    if circle is None:
        return StatusReading(None, "dial not found")
    needle = read_needle(gray, circle)
    if needle is None:
        return StatusReading(None, "needle not found", circle=circle)
    return status_reading(circle, *needle)
