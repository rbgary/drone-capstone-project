"""What is done to one captured frame for showing it: an ordered list of steps.

A step has a name and a version and turns one 8-bit grayscale image into
another. The list is a list so a later step can follow the upscale without
changing anything that runs it — the image model on upscaled frames is the
one being talked about, and is NOT decided: findings resting on drawn detail
is a team decision still open (docs/features/pipeline/enhance.txt).

PARITY: ml/upscaling-eval/methods.py (realesrgan, clahe_tiles) and
ml/upscaling-eval/chain.py (CLAHE_CLIP, CLAHE_TILES) — measured there
(RESULTS.txt, THE DISPLAY CHAIN). A change here is measured there first.

THE MODEL RUNS ON OPENCV'S dnn, not ONNX Runtime: the agent already ships
OpenCV, and on the 15 evaluation frames dnn's output was within one grey
level of ONNX Runtime's, at 581 ms a frame against 709–1100 (2026-11-04).
The file is ONNX either way, so ONNX Runtime — and with it a GPU — can run
it later behind the same `run`.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
from numpy.typing import NDArray

Gray = NDArray[np.uint8]

MODELS_DIR = Path(__file__).resolve().parents[1] / "pipeline" / "models"
REALESRGAN = "realesr-general-x4v3"
#: After the upscale — the least grain on both evaluation sets for the
#: sharpness of the rest (RESULTS.txt, WHICH CLAHE). 4×4 counts across the
#: frame, so it covers what clahe@1's 4×4 covers on the original.
CLAHE_CLIP, CLAHE_TILES = 1.5, 4


class Step(Protocol):
    name: str
    version: str

    def run(self, gray: Gray) -> Gray: ...


class ModelUnavailable(Exception):
    """The model is missing, altered, or will not load; the message says which."""


def model_entry(name: str, models_dir: Path = MODELS_DIR) -> tuple[dict[str, str], Path]:
    """The MODELS.json entry named `name`, and its file — checked against the
    entry's sha256, so a file that changed is never run under the old name."""
    try:
        entries = json.loads((models_dir / "MODELS.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ModelUnavailable(f"MODELS.json could not be read: {e}") from None
    entry = next((e for e in entries if e.get("name") == name), None)
    if entry is None:
        raise ModelUnavailable(f"MODELS.json has no {name}")
    path = models_dir / entry["file"]
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise ModelUnavailable(f"{entry['file']} is not installed") from None
    if digest != entry["sha256"]:
        raise ModelUnavailable(f"{entry['file']} is not the file MODELS.json names")
    return entry, path


def threads() -> int:
    """Half the cores, at least one: the agent beside it keeps the rest."""
    return max(1, (os.cpu_count() or 2) // 2)


@dataclass
class Upscale:
    """Real-ESRGAN ×4 (realesr-general-x4v3), then CLAHE 1.5 on 4×4 tiles."""

    name: str = f"realesrgan-x4+clahe-{CLAHE_CLIP}-tiles{CLAHE_TILES}"
    version: str = "1"
    models_dir: Path = MODELS_DIR

    def __post_init__(self) -> None:
        _, path = model_entry(REALESRGAN, self.models_dir)
        try:
            self._net = cv2.dnn.readNetFromONNX(str(path))
        except cv2.error as e:
            raise ModelUnavailable(f"{path.name} would not load: {e}") from None
        self._clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP,
                                      tileGridSize=(CLAHE_TILES, CLAHE_TILES))

    def run(self, gray: Gray) -> Gray:
        # RGB in 0..1: the gray frame repeated to three channels, and the three
        # outputs averaged back (they differ by rounding only).
        x = np.repeat(gray[None, None].astype(np.float32) / 255.0, 3, axis=1)
        self._net.setInput(x, "input")
        y = self._net.forward()[0].mean(axis=0)
        up: Gray = np.clip(y * 255.0 + 0.5, 0, 255).astype(np.uint8)
        return np.asarray(self._clahe.apply(up), dtype=np.uint8)


def default_steps() -> list[Step]:
    """The display chain, in order. Raises ModelUnavailable."""
    cv2.setNumThreads(threads())
    return [Upscale()]


def steps_key(steps: list[Step]) -> str:
    """What a frame was made with: every step's name@version, in order. A
    frame made with another key is made again."""
    return "|".join(f"{s.name}@{s.version}" for s in steps)
