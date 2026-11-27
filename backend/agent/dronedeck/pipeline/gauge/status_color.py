"""The SYSTEM STATUS gauge in full-resolution COLOUR photos — phone shots, any
distance (was systemStatusReaderColor.py).

The colour counterpart of status.py, built the way color.py is built from dial.py:

  * the dial is found with status.py's own logic (a circle with its bottom cut
    off, found below the pressure gauge when both are in view) on downscaled
    copies, small first, and mapped back to full resolution;
  * the needle is read from the hub: the rivet is found as a bright hole in the
    needle blob and the tip is the blob pixel farthest from it
    (color.read_needle_hub).
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from dronedeck.pipeline.gauge import status
from dronedeck.pipeline.gauge.color import SEARCH_SCALES, read_needle_hub, valid_circles
from dronedeck.pipeline.gauge.dial import Circle, Point, weakest_sector
from dronedeck.pipeline.gauge.status import StatusReading, status_reading

Bgr = NDArray[Any]
Gray = NDArray[Any]

#: Where the needle's rivet usually sits in the DIAL_PX dial.
HUB_PRIOR = (128.0, 128.0 * (1 + status.HUB_DROP))

#: How far a detected circle may sit from the guess below the pressure dial, in guess
#: radii (the 0.35 of the greyscale reader missed IMG_1752 by a hair).
MATCH_POS, MATCH_RADIUS = 0.6, 0.4
#: A circle found by the local search must still show the bezelled arc.
REFINE_TOP_CONTRAST = 15.0


def _top_contrast(gray: Gray, circle: Circle) -> float:
    """Face-minus-bezel contrast over the dial's upper arc; -inf when the centre is off the image
    (the status position guessed below a pressure dial can lie past the bottom of the frame)."""
    cx, cy, r = circle
    h, w = gray.shape
    if not (0 <= cx < w and 0 <= cy < h):
        return -np.inf
    return weakest_sector(gray, float(cx), float(cy), float(r), shown=status.TOP_SECTORS)


def _pressure_dial(gray: Gray, narrow: bool = False) -> Circle | None:
    """The pressure gauge above the status gauge, if the picture has both. Found with the
    colour pressure finder (looser contrast cutoff than the greyscale one, which missed
    IMG_1766). `narrow` searches only small radii, for a distant dial (radius ~8% of the frame)."""
    valid = valid_circles(gray, not narrow)
    if not valid:
        return None
    circle = max(valid, key=lambda c: c[2])
    return circle if _top_contrast(gray, status.below(circle)) > status.MIN_BELOW_CONTRAST else None


def _refine(gray: Gray, guess: Circle) -> Circle:
    """The status circle near `guess`: a local Hough search, best-bezelled top arc wins.
    The guess itself is only a fixed offset from the pressure dial, so it is a last resort."""
    gx, gy, gr = guess
    h, w = gray.shape
    x0, x1 = max(0, int(gx - 1.6 * gr)), min(w, int(gx + 1.6 * gr))
    y0, y1 = max(0, int(gy - 1.6 * gr)), min(h, int(gy + 1.6 * gr))
    if x1 - x0 < 20 or y1 - y0 < 20:
        return guess
    found = cv2.HoughCircles(cv2.medianBlur(gray[y0:y1, x0:x1], 5), cv2.HOUGH_GRADIENT, dp=1.2,
                             minDist=max(5, int(gr / 2)), param1=100, param2=15,
                             minRadius=int(0.7 * gr), maxRadius=int(1.3 * gr))
    best, best_top = guess, REFINE_TOP_CONTRAST
    for fx, fy, fr in ([] if found is None else found[0]):
        cx, cy = float(fx) + x0, float(fy) + y0
        if np.hypot(cx - gx, cy - gy) > MATCH_POS * gr * 1.5:
            continue
        top = _top_contrast(gray, (cx, cy, float(fr)))
        if top > best_top:
            best, best_top = (cx, cy, float(fr)), top
    return best


def find_dial_small(gray: Gray, narrow: bool = False) -> Circle | None:
    """status.find_dial with the colour pressure finder, a looser match to the guess
    and a local search instead of the bare guess."""
    circles = status.status_circles(gray)
    pressure = _pressure_dial(gray, narrow)
    if pressure is None:
        return circles[0] if circles else None
    guess = status.below(pressure)
    near = [c for c in circles if np.hypot(c[0] - guess[0], c[1] - guess[1]) < MATCH_POS * guess[2]
            and abs(c[2] - guess[2]) < MATCH_RADIUS * guess[2]]
    if near:
        return min(near, key=lambda c: np.hypot(c[0] - guess[0], c[1] - guess[1]))
    return _refine(gray, guess)


def find_dial_multiscale(gray: Gray) -> Circle | None:
    """find_dial_small on downscaled copies; the first scale that finds one wins.

    The narrow-radius pressure search (distant dials) only runs when the normal search has found
    nothing at any scale: it measures the dial's radius a little differently, and the reading
    depends on the radius, so photos that already worked must not change."""
    long_side = max(gray.shape)
    for narrow in (False, True):
        for target in SEARCH_SCALES:
            k = min(1.0, target / long_side)
            small = (cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
                     if k < 1 else gray)
            circle = find_dial_small(small, narrow)
            if circle:
                return circle[0] / k, circle[1] / k, circle[2] / k
            if k >= 1:
                break
    return None


def read_gauge_gray_multiscale(gray: Gray, circle: Circle | None = None,
                               hub_prior: Point | None = HUB_PRIOR) -> StatusReading:
    """The colour reader's search and needle read, on a greyscale image."""
    circle = circle or find_dial_multiscale(gray)
    if circle is None:
        return StatusReading(None, "dial not found")
    # keep the label and the cut edge out of the search
    lowest = status.cut_depth(gray, circle)
    needle = read_needle_hub(gray, circle, hub_prior, lowest)
    if needle is None:
        return StatusReading(None, "needle not found", circle=circle)
    tip, tail, _ = needle
    return status_reading(circle, tip, tail)


def read_gauge_color(bgr: Bgr, circle: Circle | None = None,
                     hub_prior: Point | None = HUB_PRIOR) -> StatusReading:
    """A colour photo, read end to end. `circle` skips the dial search."""
    return read_gauge_gray_multiscale(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), circle, hub_prior)
