"""The gauge pipeline: a batch of uploaded photos → two readings each, on one picture.

    photo bytes ─▶ decode ─▶ (sensor frame: remove crosshair)
        ─▶ PRESSURE gauge (0-600 kPa)      ─┐ find the dial, read the needle,
        ─▶ SYSTEM STATUS gauge (0-100)     ─┘ ok / warning / danger
        ─▶ ONE annotated picture with both verdicts in its top right corner

    reticle.py       the drone camera's crosshair, inpainted away
    dial.py          the dial's geometry, the scale, the greyscale pressure reader, Reading
    color.py         the multiscale dial search and hub-based needle read for colour photos
    status.py        the system status gauge (a dial with its bottom cut off) — greyscale
    status_color.py  the same, for colour photos; both find it below the pressure gauge
    annotate.py      the one picture: both dials marked, both results in the corner
    photo.py         read_photo(): the one entry point — bytes in, PhotoResult out

A second, separate pipeline from the flight's (pipeline/runner.py): its unit of
work is an uploaded photo, not a recorded flight, and it has no stages to plug
in. Like the flight pipeline it imports nothing from flight, session, api,
sync or camera (tests/pipeline/test_gauge.py holds that line).
The Supabase side — claiming a batch, downloading, uploading the result — is
sync/gauge_batches.py.
"""

from dronedeck.pipeline.gauge.photo import (
    EXTENSIONS,
    PhotoResult,
    UnreadableImage,
    read_photo,
)

__all__ = ["EXTENSIONS", "PhotoResult", "UnreadableImage", "read_photo"]
