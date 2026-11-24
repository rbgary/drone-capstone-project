"""The ONE picture a user sees for a photo: both gauges, read together.

A crop of the photo that holds the pressure dial and the system status dial,
each circled in the colour of its verdict with its needle drawn (the dials print
their own names), and in the top
right corner a panel with both results — the pressure's kPa and verdict, and
the system status's value and verdict — each line in that verdict's colour.
(From pressureReaderColor.annotate and systemStatusReaderColor.annotate, which
each drew their own picture.)
"""

from __future__ import annotations

from typing import Any, Protocol

import cv2
from numpy.typing import NDArray

from dronedeck.pipeline.gauge.dial import Circle, Point

Bgr = NDArray[Any]
Colour = tuple[int, int, int]

PURPLE = (200, 0, 170)                 # BGR, for a needle outside the scale
YELLOW = (0, 215, 255)                 # BGR, for the warning band
RED = (0, 0, 255)
GREEN = (0, 200, 0)
GREY = (150, 150, 150)
LONG_SIDE_PX = 960                      # the annotated picture's longer side
MARGIN = 1.35                           # crop this many radii around each dial: bezel, and room
PLATE = (20, 20, 20)


class DialReading(Protocol):
    """What the picture needs of either gauge's reading."""

    circle: Circle | None
    tip: Point | None
    tail: Point | None

    @property
    def state(self) -> str: ...
    @property
    def off_scale(self) -> bool: ...
    @property
    def summary(self) -> str: ...


def colour_of(reading: DialReading) -> Colour:
    if reading.off_scale:
        return PURPLE
    return {"ok": GREEN, "warning": YELLOW, "danger": RED}.get(reading.state, GREY)


def _text(img: Bgr, text: str, org: tuple[int, int], colour: Colour, scale: float,
          *, right: bool = False) -> int:
    """Text on a dark plate so it reads on any background. With `right`, `org`'s x
    is the plate's right edge. Returns the plate's height, for stacking lines."""
    font, thick = cv2.FONT_HERSHEY_SIMPLEX, 2
    (w, h), base = cv2.getTextSize(text, font, scale, thick)
    x, y = org
    if right:
        x -= w
    cv2.rectangle(img, (x - 6, y - h - 8), (x + w + 6, y + base + 4), PLATE, -1)
    cv2.putText(img, text, (x, y), font, scale, colour, thick, cv2.LINE_AA)
    return h + base + 14


def _crop_box(shape: tuple[int, ...], readings: list[DialReading]) -> tuple[int, int, int, int]:
    """The part of the photo to show: every dial found with its bezel, or the middle
    of the frame when none was."""
    h, w = shape[:2]
    circles = [r.circle for r in readings if r.circle]
    if circles:
        x0 = min(cx - MARGIN * r for cx, _, r in circles)
        x1 = max(cx + MARGIN * r for cx, _, r in circles)
        y0 = min(cy - MARGIN * r for _, cy, r in circles)
        y1 = max(cy + MARGIN * r for _, cy, r in circles)
    else:
        half = min(h, w) / 2
        x0, x1, y0, y1 = w / 2 - half, w / 2 + half, h / 2 - half, h / 2 + half
    box = (int(max(0, x0)), int(max(0, y0)), int(min(w, x1)), int(min(h, y1)))
    return box if box[2] - box[0] >= 20 and box[3] - box[1] >= 20 else (0, 0, w, h)


def annotate(bgr: Bgr, pressure: DialReading, system: DialReading, label: str = "") -> Bgr:
    """One picture: the crop holding both dials, each marked, and the results of both
    in the top right corner."""
    x0, y0, x1, y1 = _crop_box(bgr.shape, [pressure, system])
    crop = bgr[y0:y1, x0:x1]
    k = LONG_SIDE_PX / max(crop.shape[:2])
    out = cv2.resize(crop, None, fx=k, fy=k,
                     interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)

    def to_out(p: tuple[float, float]) -> tuple[int, int]:
        return int(round((p[0] - x0) * k)), int(round((p[1] - y0) * k))

    for reading in (pressure, system):
        if not reading.circle:
            continue
        colour = colour_of(reading)
        cx, cy, r = reading.circle
        cv2.circle(out, to_out((cx, cy)), int(round(r * k)), colour, 2, cv2.LINE_AA)
        if reading.tip and reading.tail:
            cv2.line(out, to_out(reading.tail), to_out(reading.tip), (255, 128, 0), 3, cv2.LINE_AA)
            cv2.circle(out, to_out(reading.tip), 6, (255, 255, 255), -1, cv2.LINE_AA)

    if label:
        _text(out, label, (10, 32), (230, 230, 230), 0.7)
    y = 32
    for name, reading in (("PRESSURE", pressure), ("SYSTEM", system)):
        y += _text(out, f"{name}  {reading.summary}", (out.shape[1] - 10, y),
                   colour_of(reading), 0.7, right=True)
    return out
