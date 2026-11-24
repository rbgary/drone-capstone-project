"""The gauge pipeline (pipeline/gauge): photo bytes → pressure and system status
readings, on one picture.

There are no real gauge photos in the repo, so these draw a pressure gauge: a bright dial in
a dark bezel, a thick arrow-shaped needle pivoting on a hub below the centre,
pointing where a given pressure would put it. The reader must give that
pressure back, from a colour photo and from a greyscale sensor frame with the
viewer's crosshair across the middle.

The real photos are the lab's check: `dronedeck gauge <folder> --out <dir>`.
"""

from __future__ import annotations

import io
import math
import subprocess
import sys

import cv2
import numpy as np
import pytest
from PIL import Image

from dronedeck.pipeline.gauge import UnreadableImage, annotate, read_photo, status, status_color
from dronedeck.pipeline.gauge.dial import Reading
from dronedeck.pipeline.gauge.photo import is_sensor_frame, load_bgr
from dronedeck.pipeline.gauge.reticle import remove_reticle
from dronedeck.pipeline.gauge.status import StatusReading
from tests.pipeline.test_conformance import FORBIDDEN


def draw_gauge(kpa: float, *, sensor: bool = False) -> np.ndarray:
    """A BGR picture of a gauge reading `kpa` (0-600 over a 240 degree sweep)."""
    w, h, r = (828, 621, 150) if sensor else (1000, 800, 200)
    img = np.full((h, w, 3), (150, 140, 130), np.uint8)
    cx, cy = w // 2 - 60, h // 2 - 20
    cv2.circle(img, (cx, cy), int(r * 1.25), (25, 25, 25), -1)        # bezel
    cv2.circle(img, (cx, cy), r, (235, 235, 235), -1)                 # face
    theta = math.radians(kpa / 600 * 240 - 120)
    hub = np.array([cx, cy + 0.27 * r])
    on_ticks = np.array([cx + 0.85 * r * math.sin(theta), cy - 0.85 * r * math.cos(theta)])
    along = (on_ticks - hub) / np.linalg.norm(on_ticks - hub)
    across = np.array([-along[1], along[0]])
    tip = hub + along * 0.75 * np.linalg.norm(on_ticks - hub)
    base = tip - along * 0.28 * r
    stem, head = 0.07 * r, 0.14 * r
    arrow = np.array([hub + across * stem, base + across * stem, base + across * head, tip,
                      base - across * head, base - across * stem, hub - across * stem], np.int32)
    cv2.fillPoly(img, [arrow], (30, 30, 30))
    cv2.circle(img, (int(hub[0]), int(hub[1])), int(0.05 * r), (220, 220, 220), -1)   # the rivet
    if sensor:
        img = cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        mid = (w // 2, h // 2)
        cv2.line(img, (mid[0] - 40, mid[1]), (mid[0] + 40, mid[1]), (0, 0, 0), 3)
        cv2.line(img, (mid[0], mid[1] - 40), (mid[0], mid[1] + 40), (0, 0, 0), 3)
    return img


def encode(img: np.ndarray, ext: str = ".png") -> bytes:
    ok, buf = cv2.imencode(ext, img)
    assert ok
    return buf.tobytes()


@pytest.mark.parametrize("kpa,state", [(150, "ok"), (300, "warning"), (450, "danger")])
def test_a_colour_photo_reads_back_its_pressure(kpa, state):
    result = read_photo(encode(draw_gauge(kpa)), "photo")
    assert result.reader == "color"
    assert result.kpa is not None and result.kpa == pytest.approx(kpa, abs=5)
    assert result.state == state


@pytest.mark.parametrize("kpa,state", [(150, "ok"), (300, "warning"), (450, "danger")])
def test_a_sensor_frame_is_read_with_its_crosshair_removed(kpa, state):
    result = read_photo(encode(draw_gauge(kpa, sensor=True)), "frame")
    assert result.kpa is not None and result.kpa == pytest.approx(kpa, abs=5)
    assert result.state == state


def test_a_jpeg_is_decoded_and_read():
    # Only the decoding is under test: the drawn dial's flat bezel makes the circle
    # search sensitive to JPEG noise, which a real gauge's photo is not.
    ok, buf = cv2.imencode(".jpg", draw_gauge(450), [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    result = read_photo(buf.tobytes(), "photo")
    assert result.kpa == pytest.approx(450, abs=8)


def test_the_annotated_picture_is_one_png():
    result = read_photo(encode(draw_gauge(450)), "photo 7")
    picture = cv2.imdecode(np.frombuffer(result.annotated_png, np.uint8), cv2.IMREAD_COLOR)
    assert max(picture.shape[:2]) == annotate.LONG_SIDE_PX
    assert result.annotated_png.startswith(b"\x89PNG")


def test_the_row_is_what_gauge_images_stores():
    row = read_photo(encode(draw_gauge(450)), "photo").to_row()
    assert set(row) == {"kpa", "raw_kpa", "state", "error", "reader", "dial_diameter_px",
                        "system_value", "system_raw_value", "system_state", "system_error",
                        "system_reader", "system_dial_diameter_px",
                        "width_px", "height_px", "detail"}
    assert row["state"] == "danger" and row["error"] is None
    assert (row["width_px"], row["height_px"]) == (1000, 800)
    assert row["dial_diameter_px"] == pytest.approx(400, abs=20)
    assert set(row["detail"]) == {"pressure", "system"}
    assert set(row["detail"]["pressure"]) == {"circle", "tip", "tail"}


def test_a_photo_with_no_gauge_is_a_result_not_an_error():
    blank = np.full((600, 800, 3), (120, 90, 60), np.uint8)
    result = read_photo(encode(blank), "wall")
    assert result.kpa is None and result.state == "unreadable"
    assert result.pressure.error == "dial not found"
    assert result.to_row()["kpa"] is None and result.to_row()["system_value"] is None
    assert result.system.state == "unreadable"
    assert result.annotated_png                       # the user still sees the photo


def test_bytes_that_are_not_an_image_raise():
    with pytest.raises(UnreadableImage):
        read_photo(b"this is not a picture", "x")


def test_exif_rotation_is_applied():
    upright = draw_gauge(300)
    buf = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6                                   # "rotate 90 degrees clockwise to view"
    Image.fromarray(cv2.cvtColor(cv2.rotate(upright, cv2.ROTATE_90_COUNTERCLOCKWISE),
                                 cv2.COLOR_BGR2RGB)).save(buf, "JPEG", exif=exif)
    assert load_bgr(buf.getvalue()).shape == upright.shape


def test_only_small_grey_frames_are_sensor_frames():
    assert is_sensor_frame(draw_gauge(300, sensor=True))
    assert not is_sensor_frame(draw_gauge(300))                      # colour
    big = np.full((2000, 2400, 3), 128, np.uint8)
    assert not is_sensor_frame(big)                                  # grey, but a full photo


@pytest.mark.parametrize("kpa,state,label", [
    (0, "ok", "ok"), (199, "ok", "ok"), (200, "ok", "ok"), (201, "warning", "WARNING"),
    (399, "warning", "WARNING"), (400, "danger", "DANGER"), (600, "danger", "DANGER"),
])
def test_the_bands(kpa, state, label):
    reading = Reading(float(kpa))
    assert (reading.state, reading.label) == (state, label)


def test_no_reading_has_no_band():
    reading = Reading(None, "dial not found")
    assert (reading.state, reading.faulty, reading.warning) == ("unreadable", None, None)
    assert reading.label == "ERROR: dial not found"


def test_remove_reticle_leaves_a_clean_frame_alone():
    flat = np.full((621, 828), 100, np.uint8)
    assert np.array_equal(remove_reticle(flat), flat)


def test_the_gauge_pipeline_imports_nothing_from_the_flight_code():
    code = ("import sys, dronedeck.pipeline.gauge; "
            "print('\\n'.join(m for m in sys.modules if m.startswith('dronedeck')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    assert not [m for m in out.stdout.split() if m.startswith(FORBIDDEN)]


# ── the system status gauge, and the one picture ────────────────────────────

def circle_reading(value, cx=300.0, cy=300.0, r=120.0):
    return StatusReading(value, None, value, (cx, cy, r), (cx + 30, cy - 80), (cx, cy + 20))


@pytest.mark.parametrize("value,state,label", [
    (0, "ok", "ok"), (30, "ok", "ok"), (31, "warning", "WARNING"),
    (69, "warning", "WARNING"), (70, "danger", "DANGER"), (100, "danger", "DANGER"),
])
def test_the_system_status_bands(value, state, label):
    reading = StatusReading(float(value))
    assert (reading.state, reading.label) == (state, label)


def test_a_system_needle_off_its_scale_is_unreadable_but_remembered():
    reading = status.status_reading((100.0, 100.0, 50.0), (100.0, 160.0), (100.0, 100.0))
    assert reading.value is None and reading.raw is not None and reading.off_scale
    assert reading.state == "unreadable" and "out of range" in (reading.error or "")


def test_both_readings_land_in_the_top_right_of_one_picture(monkeypatch):
    """A danger pressure (red) and a warning system status (yellow), stacked top right."""
    monkeypatch.setattr(status, "read_gauge", lambda gray: circle_reading(50.0))
    monkeypatch.setattr(status_color, "read_gauge_color", lambda bgr: circle_reading(50.0))
    result = read_photo(encode(draw_gauge(450)), "both")
    assert result.state == "danger" and result.system.state == "warning"
    picture = cv2.imdecode(np.frombuffer(result.annotated_png, np.uint8), cv2.IMREAD_COLOR)
    w = picture.shape[1]
    corner = picture[:110, w - 420:]
    red_rows = np.nonzero(np.all(corner == annotate.RED, axis=-1))[0]
    yellow_rows = np.nonzero(np.all(corner == annotate.YELLOW, axis=-1))[0]
    assert len(red_rows) > 50 and len(yellow_rows) > 50   # a line of each, in its verdict's colour
    assert red_rows.mean() < yellow_rows.mean()           # pressure above system status


def test_the_crop_holds_both_dials():
    pressure = Reading(450.0, None, 450.0, (500.0, 300.0, 100.0), (520.0, 230.0), (500.0, 320.0))
    system = circle_reading(15.0, cx=500.0, cy=600.0, r=85.0)
    x0, y0, x1, y1 = annotate._crop_box((1000, 1000), [pressure, system])
    assert x0 < 500 - 100 and x1 > 500 + 100
    assert y0 < 300 - 100 and y1 > 600 + 85


def test_no_dial_found_still_gives_a_picture_with_both_verdicts():
    blank = np.full((600, 800, 3), (120, 90, 60), np.uint8)
    picture = cv2.imdecode(np.frombuffer(read_photo(encode(blank), "wall").annotated_png,
                                         np.uint8), cv2.IMREAD_COLOR)
    assert picture.shape[0] == picture.shape[1] == annotate.LONG_SIDE_PX   # the middle square
