"""The dial's geometry and the greyscale needle reader (was pressureReader.py).

Geometric baseline, no training: remove the reticle, find the dial (the largest
bright disc with a dark bezel all round), cut it out at a fixed size, find the
dark arrow-shaped needle, turn its angle into kPa, and compare with the fault
band.

The dial sweeps 240 degrees: 0 kPa at -120 deg from straight up, 600 kPa at +120.
Everything the colour reader (color.py) shares with this one lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from dronedeck.pipeline.gauge.reticle import remove_reticle

Gray = NDArray[Any]
Circle = tuple[float, float, float]            # cx, cy, r in the source image
Point = tuple[float, float]

FAULT_MIN, FAULT_MAX = 400.0, 600.0     # inclusive
WARN_MIN, WARN_MAX = 200.0, 400.0       # exclusive: the middle third of the scale
#: Half the dial's sweep, from straight up (0 kPa at -120, 600 at +120).
SWEEP_DEG = 120.0
#: Where the scale's ticks sit, as a fraction of the dial radius.
TICK_RING = 0.85
FULL_SCALE = 600.0
RANGE_TOLERANCE = 0.0                  # kPa of slack past 0 and 600
MIN_BEZEL_CONTRAST = 10               # grey levels, face over bezel, in every sector
DIAL_PX = 256                          # the dial is resampled to this many px across


@dataclass
class Reading:
    kpa: float | None                  # None when there is no valid reading (see error)
    error: str | None = None           # why there is no reading
    raw_kpa: float | None = None       # the needle's reading before the range check
    circle: Circle | None = None       # cx, cy, r in the source image
    tip: Point | None = None           # needle tip in the source image
    tail: Point | None = None          # needle stem end (the hub, for the colour reader)

    @property
    def faulty(self) -> bool | None:
        return None if self.kpa is None else FAULT_MIN <= self.kpa <= FAULT_MAX

    @property
    def warning(self) -> bool | None:
        return None if self.kpa is None else WARN_MIN < self.kpa < WARN_MAX

    @property
    def state(self) -> str:
        """What the web stores: unreadable | danger | warning | ok."""
        if self.error or self.kpa is None:
            return "unreadable"
        if self.faulty:
            return "danger"
        return "warning" if self.warning else "ok"

    @property
    def label(self) -> str:
        if self.error:
            return f"ERROR: {self.error}"
        return {"danger": "DANGER", "warning": "WARNING"}.get(self.state, "ok")

    @property
    def off_scale(self) -> bool:
        """The needle was found, but outside 0-600."""
        return self.raw_kpa is not None and self.kpa is None

    @property
    def summary(self) -> str:
        return self.label if self.kpa is None else f"{self.kpa:.0f} kPa  {self.label}"


def needle_value(circle: Circle, tip: Point, tail: Point,
                 full_scale: float) -> tuple[float | None, float | None, str | None]:
    """Where the needle points on a 0-`full_scale` dial that sweeps 240 degrees:
    (value clipped to the scale, the raw value, why there is none).
    Shared by the pressure gauge (0-600) and the system status gauge (0-100)."""
    angle = scale_angle(circle, tip, tail)
    if angle is None:
        return None, None, "needle not found"
    raw = (angle + SWEEP_DEG) / (2 * SWEEP_DEG) * full_scale
    if not -RANGE_TOLERANCE <= raw <= full_scale + RANGE_TOLERANCE:
        return None, raw, f"needle out of range ({raw:.0f})"
    return float(np.clip(raw, 0, full_scale)), raw, None


def reading_from_tip(circle: Circle, tip: Point, tail: Point) -> Reading:
    """The needle's tip and tail, as a reading on the 0-600 scale."""
    kpa, raw, error = needle_value(circle, tip, tail, FULL_SCALE)
    if raw is None:
        return Reading(None, error, circle=circle)
    return Reading(kpa, error, raw, circle, tip, tail)


def weakest_sector(gray: Gray, cx: float, cy: float, r: float, sectors: int = 12,
                   shown: range | None = None) -> float:
    """Face-minus-bezel brightness in the worst of `sectors` wedges around the circle.

    A real dial has a dark bezel all the way round; a bright patch that merely has
    one circular edge (the foil panel) is dark on one side only and scores low.
    `shown` limits the check to those wedges (0 = left, clockwise): the system
    status dial has its bottom cut off, so only its upper arc is bezelled."""
    h, w = gray.shape
    x0, x1 = max(0, int(cx - 1.4 * r)), min(w, int(cx + 1.4 * r) + 1)
    y0, y1 = max(0, int(cy - 1.4 * r)), min(h, int(cy + 1.4 * r) + 1)
    yy, xx = np.mgrid[y0:y1, x0:x1]
    dx, dy = xx - cx, yy - cy
    dist = np.hypot(dx, dy)
    wedge = ((np.arctan2(dy, dx) + np.pi) / (2 * np.pi) * sectors).astype(int) % sectors
    patch = gray[y0:y1, x0:x1].astype(np.float32)
    worst = np.inf
    for k in range(sectors):
        if shown is not None and k not in shown:
            continue
        here = wedge == k
        face = here & (dist > 0.3 * r) & (dist < 0.8 * r)
        bezel = here & (dist > 1.1 * r) & (dist < 1.35 * r)
        if not face.any() or not bezel.any():
            return -np.inf                          # the circle runs off the image
        worst = min(worst, float(patch[face].mean() - patch[bezel].mean()))
    return float(worst)


def find_dial(gray: Gray) -> Circle | None:
    """The pressure gauge: the largest circle with a bright face and a dark bezel all round."""
    h = gray.shape[0]
    blur = cv2.medianBlur(gray, 5)
    found = cv2.HoughCircles(blur, cv2.HOUGH_GRADIENT, dp=1.2, minDist=h // 8,
                             param1=100, param2=35,
                             minRadius=max(15, h // 30), maxRadius=int(h * 0.6))
    if found is None:
        return None
    best, best_score = None, 0.0
    for cx, cy, r in found[0][:15]:
        weakest = weakest_sector(gray, float(cx), float(cy), float(r))
        if weakest > MIN_BEZEL_CONTRAST and weakest * r > best_score:
            best, best_score = (float(cx), float(cy), float(r)), weakest * r
    return best


@dataclass(frozen=True)
class DialView:
    """The dial resampled to DIAL_PX across, and the maps back to the source."""

    image: Gray
    dist: NDArray[np.float64]           # each pixel's distance from the dial centre
    inside: NDArray[np.bool_]           # where the needle may be: not the tick ring or bezel
    scale: float                        # dial px per source px
    circle: Circle

    def to_source(self, p: Point) -> Point:
        cx, cy, _ = self.circle
        return ((p[0] - DIAL_PX / 2) / self.scale + cx, (p[1] - DIAL_PX / 2) / self.scale + cy)

    @property
    def face(self) -> float:
        return float(np.median(self.image[self.dist < 0.9 * (DIAL_PX / 2)]))


def dial_view(gray: Gray, circle: Circle, lowest: float = 1.0) -> DialView:
    """Cut the dial out at a fixed size. `lowest` is how far below the dial
    centre to look, as a fraction of the radius; a dial with a printed label or
    a cut-off bottom passes less than 1 to keep that out of the search."""
    cx, cy, r = circle
    s = DIAL_PX / (2 * r)
    matrix = np.array([[s, 0, DIAL_PX / 2 - cx * s], [0, s, DIAL_PX / 2 - cy * s]])
    dial = cv2.warpAffine(gray, matrix, (DIAL_PX, DIAL_PX),
                          flags=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC,
                          borderMode=cv2.BORDER_REPLICATE)
    c = DIAL_PX / 2
    yy, xx = np.mgrid[0:DIAL_PX, 0:DIAL_PX]
    dist = np.hypot(xx - c, yy - c)
    inside = (dist < 0.72 * c) & (yy - c < lowest * c)      # leave the tick ring and bezel out
    return DialView(dial, dist, inside, s, circle)


def read_needle(gray: Gray, circle: Circle, lowest: float = 1.0,
                dark_ratio: float = 0.6) -> tuple[float, Point, Point] | None:
    """Return (angle in degrees clockwise from straight up, tip and tail in source px).

    `dark_ratio` is how much darker than the face a pixel must be to count as needle."""
    view = dial_view(gray, circle, lowest)
    dark: Gray = ((view.image < view.face * dark_ratio) & view.inside).astype(np.uint8)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(dark)
    if count < 2:
        return None
    needle = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[needle, cv2.CC_STAT_AREA] < 0.02 * DIAL_PX ** 2:
        return None
    ys, xs = np.nonzero(labels == needle)
    pts = np.column_stack([xs, ys]).astype(np.float64)
    pts -= pts.mean(axis=0)
    axis = np.linalg.eigh(np.cov(pts.T))[1][:, -1]          # the arrow's long axis
    along, across = pts @ axis, pts @ np.array([-axis[1], axis[0]])
    # The arrowhead is far wider than the stem: it sits where the needle is widest.
    span = along.max() - along.min() + 1e-9
    bins = np.minimum(((along - along.min()) / span * 10).astype(int), 9)
    widths = np.array([np.ptp(across[bins == k]) if (bins == k).sum() > 3 else 0.0
                       for k in range(10)])
    if np.argmax(widths) < 5:
        axis, along = -axis, -along
    centre = np.array([xs.mean(), ys.mean()])
    tip = centre + axis * along.max()
    tail = centre + axis * along.min()
    angle = float(np.degrees(np.arctan2(axis[0], -axis[1])))
    return angle, view.to_source((tip[0], tip[1])), view.to_source((tail[0], tail[1]))


def scale_angle(circle: Circle, tip: Point, tail: Point) -> float | None:
    """Where the needle points on the scale: extend the arrow through its tip to the tick
    ring and take that spot's angle around the dial centre, clockwise from straight up.

    The arrow pivots on a hub below the dial centre, so neither its own axis angle nor the
    tip's angle around the centre matches what a person reads off the printed scale."""
    cx, cy, r = circle
    centre, tip_p = np.array([cx, cy]), np.array(tip)
    direction = tip_p - np.array(tail)
    direction /= np.linalg.norm(direction)
    offset = tip_p - centre
    b = float(offset @ direction)
    disc = b * b - (float(offset @ offset) - (TICK_RING * r) ** 2)
    if disc < 0:
        return None
    hit = tip_p + (-b + np.sqrt(disc)) * direction
    return float(np.degrees(np.arctan2(hit[0] - cx, cy - hit[1])))


def read_gauge(gray: Gray) -> Reading:
    """A sensor frame (greyscale, with the viewer's crosshair), read end to end."""
    gray = remove_reticle(gray)
    circle = find_dial(gray)
    if circle is None:
        return Reading(None, "dial not found")
    needle = read_needle(gray, circle)
    if needle is None:
        return Reading(None, "needle not found", circle=circle)
    _, tip, tail = needle
    return reading_from_tip(circle, tip, tail)
