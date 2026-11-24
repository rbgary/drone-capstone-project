"""One uploaded photo in, both gauge readings and ONE annotated picture out.

The photo shows two gauges, the pressure gauge with the system status gauge
below it. Both are read from the same photo and drawn on the same picture
(annotate.py). This is the gauge pipeline's whole job, with no knowledge of
Supabase, the radio or the flight: bytes in, a PhotoResult out.
gauge_worker.py moves the bytes; this decides what they say.

Which reader runs, for each gauge:

  * a SENSOR FRAME (greyscale PNG, at most 1600 px, crosshair in the middle):
    the crosshair is removed once, and the gauge's greyscale reader (dial.py,
    status.py) reads it. If that finds nothing, the multiscale reader gets a go.
  * anything else (a phone's colour photo, HEIC/JPG/PNG, any distance): the
    gauge's multiscale colour reader (color.py, status_color.py).

Images are decoded with their EXIF rotation applied, at full resolution.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageOps

from dronedeck.pipeline.gauge import color, dial, status, status_color
from dronedeck.pipeline.gauge.annotate import annotate
from dronedeck.pipeline.gauge.dial import Reading
from dronedeck.pipeline.gauge.reticle import remove_reticle
from dronedeck.pipeline.gauge.status import StatusReading

Bgr = NDArray[Any]

#: File types the web may upload (the bucket's allow-list says the same).
EXTENSIONS = (".png", ".jpg", ".jpeg", ".heic")
#: A greyscale image no longer than this is the drone camera's own frame, not a
#: photo that happens to be black and white.
SENSOR_FRAME_MAX_SIDE = 1600
#: How far apart a pixel's colour channels may be, on average, to be "grey".
GREY_TOLERANCE = 2.0


class UnreadableImage(ValueError):
    """The bytes are not an image this pipeline can open."""


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)


def _geometry(reading: Reading | StatusReading) -> dict[str, Any]:
    return {"circle": reading.circle, "tip": reading.tip, "tail": reading.tail}


@dataclass
class PhotoResult:
    pressure: Reading
    system: StatusReading
    reader: str                                   # pressure's: "sensor" | "color"
    system_reader: str                            # system status's
    width: int
    height: int
    annotated_png: bytes = field(repr=False, default=b"")

    @property
    def kpa(self) -> float | None:
        return self.pressure.kpa

    @property
    def state(self) -> str:
        return self.pressure.state

    def to_row(self) -> dict[str, Any]:
        """The columns of public.gauge_images this result fills (migration 20261123000022)."""
        p, s = self.pressure, self.system
        return {
            "kpa": _round(p.kpa),
            "raw_kpa": _round(p.raw_kpa),
            "state": p.state,
            "error": p.error,
            "reader": self.reader,
            "dial_diameter_px": None if p.circle is None else round(p.circle[2] * 2),
            "system_value": _round(s.value),
            "system_raw_value": _round(s.raw),
            "system_state": s.state,
            "system_error": s.error,
            "system_reader": self.system_reader,
            "system_dial_diameter_px": None if s.circle is None else round(s.circle[2] * 2),
            "width_px": self.width,
            "height_px": self.height,
            "detail": {"pressure": _geometry(p), "system": _geometry(s)},
        }


def load_bgr(data: bytes) -> Bgr:
    """Decode an image file (PNG, JPG, HEIC) to BGR, rotated upright by its EXIF."""
    from pillow_heif import register_heif_opener  # lazy: only HEIC needs it
    register_heif_opener()
    try:
        with Image.open(io.BytesIO(data)) as opened:
            img = ImageOps.exif_transpose(opened).convert("RGB")
    except Exception as e:
        raise UnreadableImage(f"not an image ({type(e).__name__})") from e
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def is_sensor_frame(bgr: Bgr) -> bool:
    if max(bgr.shape[:2]) > SENSOR_FRAME_MAX_SIDE:
        return False
    channels = bgr.astype(np.int16)
    spread = (np.abs(channels[..., 0] - channels[..., 1])
              + np.abs(channels[..., 1] - channels[..., 2]))
    return float(spread.mean()) < GREY_TOLERANCE


def read_photo(data: bytes, label: str = "") -> PhotoResult:
    """Read the pressure gauge and the system status gauge in one image file.

    Never raises for a photo with no readable gauge — that is a result
    (state "unreadable", with the reason, per gauge). Raises UnreadableImage
    only when the bytes are not an image at all."""
    bgr = load_bgr(data)
    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if is_sensor_frame(bgr):
        gray = remove_reticle(gray)
        bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        pressure, reader = dial.read_gauge(gray), "sensor"
        if pressure.kpa is None:
            second = color.read_gauge_gray_multiscale(gray)
            if second.kpa is not None:
                pressure, reader = second, "color"
        system, system_reader = status.read_gauge(gray), "sensor"
        if system.value is None:
            second_status = status_color.read_gauge_gray_multiscale(gray)
            if second_status.value is not None:
                system, system_reader = second_status, "color"
    else:
        pressure, reader = color.read_gauge_color(bgr), "color"
        system, system_reader = status_color.read_gauge_color(bgr), "color"
    picture = annotate(bgr, pressure, system, label)
    ok, encoded = cv2.imencode(".png", picture)
    if not ok:                                   # pragma: no cover — PNG always encodes
        raise UnreadableImage("could not encode the annotated picture")
    return PhotoResult(pressure, system, reader, system_reader, w, h, encoded.tobytes())
