"""Remove the viewer's crosshair from a sensor frame (was imageReticleTest.py).

The drone camera's frames carry a thin crosshair at the centre. remove_reticle()
inpaints its arms and touches nothing else, so the image keeps its size. The
constants are the source's, for a frame 828 px wide (scaled for other sizes).
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
from numpy.typing import NDArray

Gray = NDArray[Any]

#: The reticle: arms within this many px of the centre, this thick, at the
#: SOURCE's scale of 828 px wide (scaled for other sizes).
RETICLE_REACH = 44
RETICLE_HALF_WIDTH = 4
#: A pixel this far from its neighbourhood's median, on an arm, is the reticle.
RETICLE_DELTA = 18


def remove_reticle(gray: Gray) -> Gray:
    """Inpaint the crosshair's thin arms at the centre; the rest is untouched."""
    h, w = gray.shape
    k = w / 828.0
    reach, half = int(RETICLE_REACH * k) + 1, max(1, int(round(RETICLE_HALF_WIDTH * k)))
    cx, cy = w // 2, h // 2
    arms = np.zeros_like(gray, dtype=bool)
    arms[cy - half:cy + half + 1, cx - reach:cx + reach + 1] = True   # horizontal arm
    arms[cy - reach:cy + reach + 1, cx - half:cx + half + 1] = True   # vertical arm
    side = int(round(15 * k)) | 1
    background = cv2.medianBlur(gray, max(3, side))
    differs = np.abs(gray.astype(np.int16) - background.astype(np.int16)) > RETICLE_DELTA
    mask: Gray = (arms & differs).astype(np.uint8) * 255
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
    if not mask.any():
        return gray
    out: Gray = cv2.inpaint(gray, mask, 3, cv2.INPAINT_TELEA)
    return out
