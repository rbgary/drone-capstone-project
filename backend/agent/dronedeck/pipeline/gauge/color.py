"""Read the gauge in full-resolution COLOUR photos — phone shots, any distance
(was pressureReaderColor.py).

dial.py keeps the geometry; this module only changes how the dial is found and
how the needle is read:

  * the dial is searched for at several scales, small first, so a gauge that is
    only ~150 px across in a 1000 px frame is still caught;
  * the needle is read straight from the full-resolution pixels (dial_view
    resamples the dial to a fixed size itself), from its hub.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

from dronedeck.pipeline.gauge.dial import (
    DIAL_PX,
    Circle,
    Point,
    Reading,
    dial_view,
    reading_from_tip,
    weakest_sector,
)

Bgr = NDArray[Any]
Gray = NDArray[Any]

SEARCH_SCALES = (1000, 1400, 1800)      # long-side px to search the dial at, small first
#: Grey levels; the greyscale reader's 10 sat right on IMG_1766.
MIN_BEZEL_CONTRAST_COLOR = 5
HUB_PRIOR = (130.0, 162.0)              # where the needle's rivet usually sits in the DIAL_PX dial
                                        # (median of 92 detections, spread ~3 px); used when the
                                        # rivet is not seen


def valid_circles(gray: Gray, wide: bool) -> list[Circle]:
    """Hough circles that have a dark bezel all round, like dial.find_dial but with a
    looser contrast cutoff.

    `wide` is the original search: radii from h/30 to 0.6h, best 15 candidates. A distant dial
    (radius ~8% of the frame) gets outvoted in a range that wide, so `not wide` searches only
    4-14% of the height."""
    h = gray.shape[0]
    lo, hi = (max(15, h // 30), int(h * 0.6)) if wide else (int(h * 0.04), int(h * 0.14))
    found = cv2.HoughCircles(cv2.medianBlur(gray, 5), cv2.HOUGH_GRADIENT, dp=1.2, minDist=h // 8,
                             param1=100, param2=35, minRadius=lo, maxRadius=hi)
    if found is None:
        return []
    top = found[0][:15] if wide else found[0]
    return [(float(cx), float(cy), float(r)) for cx, cy, r in top
            if weakest_sector(gray, float(cx), float(cy), float(r)) > MIN_BEZEL_CONTRAST_COLOR]


def find_dial_multiscale(gray: Gray) -> Circle | None:
    """Search the dial on downscaled copies and map the circle back to full size.

    The largest valid circle at the first scale that has any wins, so the radius of a dial
    stays the one measured at that scale (the reading depends on it, and other scales can
    measure the black bezel's outer edge instead of the face). Two exceptions:
      * a larger dial directly ABOVE it (same column) at the NEXT scale replaces it: a distant
        pressure dial can be missed at the smaller scale while the smaller status dial below
        it is not. Other large circles (the green push button, say) must not win this way;
      * if the wide search finds nothing at any scale, a narrow radius band is tried."""
    long_side = max(gray.shape)

    def at(target: int, wide: bool) -> list[Circle]:
        k = min(1.0, target / long_side)
        small = cv2.resize(gray, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else gray
        return [(cx / k, cy / k, r / k) for cx, cy, r in valid_circles(small, wide)]

    for i, target in enumerate(SEARCH_SCALES):
        circles = at(target, True)
        if not circles:
            continue
        best = max(circles, key=lambda c: c[2])
        if i + 1 < len(SEARCH_SCALES):
            # the pressure dial sits directly above the status dial, in the same column
            bigger = [c for c in at(SEARCH_SCALES[i + 1], True)
                      if c[2] > 1.1 * best[2] and abs(c[0] - best[0]) < 0.5 * c[2]
                      and c[1] < best[1] - 1.2 * c[2]]
            if bigger:
                best = max(bigger, key=lambda c: c[2])
        return best
    for target in SEARCH_SCALES:
        circles = at(target, False)
        if circles:
            return max(circles, key=lambda c: c[2])
    return None


def read_needle_hub(gray: Gray, circle: Circle, hub_prior: Point | None = None,
                    lowest: float = 1.0) -> tuple[Point, Point, Point] | None:
    """Find the needle and read it from its hub.

    1. The dial is resampled to DIAL_PX; the needle is whatever is darker than an Otsu
       threshold chosen inside the dial (clamped, so a mid-grey needle still counts).
    2. The silver rivet shows up as a bright hole inside the needle blob: that is the hub.
       When no hole is found, `hub_prior` (the hub's usual spot, in dial pixels) is used.
    3. The tip is the blob pixel farthest from the hub; the arrow is too chunky for its
       principal axis to say which way it points.

    Returns (tip, hub, hub_in_dial_px), the first two in source pixels."""
    view = dial_view(gray, circle, lowest)
    dial = view.image
    face = view.face
    otsu, _ = cv2.threshold(dial[view.inside].reshape(-1, 1), 0, 255,
                            cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    thresh = float(np.clip(otsu, 0.45 * face, 0.85 * face))
    dark: Gray = ((dial < thresh) & view.inside).astype(np.uint8)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(dark)
    if count < 2:
        return None
    needle = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    if stats[needle, cv2.CC_STAT_AREA] < 0.02 * DIAL_PX ** 2:
        return None
    blob = (labels == needle).astype(np.uint8)
    ys, xs = np.nonzero(blob)

    contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(blob)
    cv2.drawContours(filled, contours, -1, 1, -1)
    holes = (filled & (1 - blob)).astype(np.uint8)
    n_h, h_labels, h_stats, h_cent = cv2.connectedComponentsWithStats(holes)
    hub = None
    best = -1.0
    for k in range(1, n_h):
        if 15 <= h_stats[k, cv2.CC_STAT_AREA] <= 700:
            brightness = float(dial[h_labels == k].mean())
            if brightness > best:
                best, hub = brightness, h_cent[k]
    if hub is None:
        if hub_prior is None:
            return None
        hub = np.array(hub_prior, dtype=np.float64)
    pts = np.column_stack([xs, ys]).astype(np.float64)
    d = np.hypot(*(pts - hub).T)
    tip = pts[d >= 0.97 * d.max()].mean(axis=0)
    return (view.to_source((tip[0], tip[1])), view.to_source((hub[0], hub[1])),
            (float(hub[0]), float(hub[1])))


def read_gauge_color(bgr: Bgr, circle: Circle | None = None,
                     hub_prior: Point | None = HUB_PRIOR) -> Reading:
    """A colour photo, read end to end. `circle` skips the dial search."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return read_gauge_gray_multiscale(gray, circle, hub_prior)


def read_gauge_gray_multiscale(gray: Gray, circle: Circle | None = None,
                               hub_prior: Point | None = HUB_PRIOR) -> Reading:
    """The colour reader's search and needle read, on a greyscale image."""
    circle = circle or find_dial_multiscale(gray)
    if circle is None:
        return Reading(None, "dial not found")
    needle = read_needle_hub(gray, circle, hub_prior)
    if needle is None:
        return Reading(None, "needle not found", circle=circle)
    tip, tail, _ = needle
    return reading_from_tip(circle, tip, tail)
