"""The candidates, each a function from an 8-bit grayscale frame to an 8-bit
grayscale frame `scale` times as wide. Only runtime-legal libraries here —
numpy, OpenCV contrib, ONNX Runtime (contract rule 11) — so whichever wins
moves into stages/enhance/ as it was measured.

THE WINNER'S STEPS ARE IMPORTED FROM espcn_steps.py (runtime, below): the
ESPCN call, CLAHE and the edge sharpen exactly as #103 wrote them for the
agent. The agent ships clahe@1 instead; espcn_steps.py says why.

Bicubic is not a candidate. It is the floor: an upscaler that cannot beat
plain interpolation at its own scale has not earned its model file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache

import cv2
import espcn_steps as runtime
import numpy as np
import onnxruntime as ort
from common import MODELS

Fn = Callable[[np.ndarray], np.ndarray]


@dataclass(frozen=True)
class Method:
    name: str
    scale: int
    fn: Fn
    family: str  # "baseline" | "classical" | "sr-cnn" | "sr-gan"
    model_files: tuple[str, ...] = field(default=())

    def model_mb(self) -> float:
        return sum((MODELS / f).stat().st_size for f in self.model_files) / 1e6


# ── bicubic: the floor ──────────────────────────────────────────────────────


def bicubic(scale: int) -> Fn:
    def run(img: np.ndarray) -> np.ndarray:
        h, w = img.shape
        return cv2.resize(img, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)

    return run


# ── CLAHE + unsharp mask: the baseline to beat (dpp-enhance.txt) ────────────
# clahe(2.0, 0.6) is OpenCV's usual clip on 8×8 tiles with a 1-px Gaussian
# unsharp at 0.6 — untuned, and it turns sensor noise into grain. The gentler
# pairs are the sweep of 2026-10-07. CLAHE changes contrast on purpose, which
# PSNR and SSIM count as error — judge it by the sheets as much as the table.

def clahe(clip: float, unsharp: float) -> Fn:
    def run(img: np.ndarray) -> np.ndarray:
        out = runtime.clahe(img, clip)
        if unsharp == 0:
            return out
        blur = cv2.GaussianBlur(out, (0, 0), runtime.UNSHARP_SIGMA)
        return cv2.addWeighted(out, 1 + unsharp, blur, -unsharp, 0)

    return run


clahe_unsharp = clahe(2.0, 0.6)


def then(first: Fn, second: Fn) -> Fn:
    return lambda img: second(first(img))


# ── denoise first, and sharpen edges only: CLAHE's look with less grain ─────
# CLAHE and unsharp boost sensor noise as readily as edges — the "flat texture"
# column. Non-local means on the native frame removes noise before anything
# amplifies it; h is its strength in grey levels (OpenCV's default is 3).


def nlm(h: float) -> Fn:
    return lambda img: cv2.fastNlMeansDenoising(img, None, h, 7, 21)


# Unsharp weighted by edge strength — runtime.edge_sharpen, whose comment
# gives the measurement behind its thresholds.


def edge_sharpen(amount: float) -> Fn:
    return lambda img: runtime.edge_sharpen(img, amount)


# ── FSRCNN / ESPCN: OpenCV dnn_superres ─────────────────────────────────────
# Both were trained on the Y channel of colour photos; OpenCV converts BGR to
# YCrCb, upscales Y with the network and Cr/Cb bicubically. A gray frame
# repeated to BGR has flat chroma, so Y is the whole answer.


@cache
def _superres(algo: str, scale: int) -> cv2.dnn_superres.DnnSuperResImpl:
    sr = cv2.dnn_superres.DnnSuperResImpl_create()
    sr.readModel(str(MODELS / f"{algo.upper()}_x{scale}.pb"))
    sr.setModel(algo, scale)
    return sr


def superres(algo: str, scale: int) -> Fn:
    return lambda img: runtime.upscale(_superres(algo, scale), img)


# ── Real-ESRGAN realesr-general-x4v3: ONNX Runtime ──────────────────────────
# RGB in 0..1; the gray frame is repeated to three channels and the three
# outputs averaged back (they differ by rounding only).


@cache
def _realesrgan() -> ort.InferenceSession:
    return ort.InferenceSession(
        str(MODELS / "realesr-general-x4v3.onnx"), providers=["CPUExecutionProvider"]
    )


def run_realesrgan(session: ort.InferenceSession, img: np.ndarray) -> np.ndarray:
    """On any session — chain.py passes one on the GPU."""
    x = np.repeat(img[None, None].astype(np.float32) / 255.0, 3, axis=1)
    y = session.run(None, {"input": x})[0][0].mean(axis=0)
    return np.clip(y * 255.0 + 0.5, 0, 255).astype(np.uint8)


def realesrgan(img: np.ndarray) -> np.ndarray:
    return run_realesrgan(_realesrgan(), img)


# ── Real-ESRGAN, then CLAHE: the display chain (2026-11-04, the team's pick) ─
# Upscale first, so CLAHE does not lift noise the network then draws as
# texture. The grid is counted across the frame, so 4×4 on the ×4 frame covers
# the same part of the scene as clahe@1's 4×4 on the original; 8×8 is
# espcn_steps.py's.


def clahe_tiles(clip: float, tiles: int) -> Fn:
    return lambda img: cv2.createCLAHE(clipLimit=clip, tileGridSize=(tiles, tiles)).apply(img)


# ── the registry ────────────────────────────────────────────────────────────

METHODS: list[Method] = [
    Method("bicubic-x2", 2, bicubic(2), "baseline"),
    Method("bicubic-x4", 4, bicubic(4), "baseline"),
    Method("clahe-unsharp", 1, clahe_unsharp, "classical"),
    Method("clahe-unsharp+bicubic-x2", 2, then(clahe_unsharp, bicubic(2)), "classical"),
    Method("fsrcnn-x2", 2, superres("fsrcnn", 2), "sr-cnn", ("FSRCNN_x2.pb",)),
    Method("fsrcnn-x4", 4, superres("fsrcnn", 4), "sr-cnn", ("FSRCNN_x4.pb",)),
    Method("espcn-x2", 2, superres("espcn", 2), "sr-cnn", ("ESPCN_x2.pb",)),
    Method("espcn-x4", 4, superres("espcn", 4), "sr-cnn", ("ESPCN_x4.pb",)),
    Method(
        "fsrcnn-x2+clahe-unsharp",
        2,
        then(superres("fsrcnn", 2), clahe_unsharp),
        "sr-cnn",
        ("FSRCNN_x2.pb",),
    ),
    # ESPCN ×2 for resolution, then contrast for visibility, at three strengths
    *(
        Method(
            f"espcn-x2+clahe-{clip}-{unsharp}",
            2,
            then(superres("espcn", 2), clahe(clip, unsharp)),
            "sr-cnn",
            ("ESPCN_x2.pb",),
        )
        for clip, unsharp in [(1.0, 0.0), (1.5, 0.3), (2.0, 0.6)]
    ),
    # the team's pick (espcn-x2+clahe-2.0-0.6), with its grain attacked two ways
    *(
        Method(
            f"nlm-{h}+espcn-x2+clahe-2.0-0.6",
            2,
            then(nlm(h), then(superres("espcn", 2), clahe(2.0, 0.6))),
            "sr-cnn",
            ("ESPCN_x2.pb",),
        )
        for h in (3, 5)
    ),
    *(
        Method(
            f"espcn-x2+clahe-{clip}+edge-{amount}",
            2,
            then(superres("espcn", 2), then(clahe(clip, 0), edge_sharpen(amount))),
            "sr-cnn",
            ("ESPCN_x2.pb",),
        )
        # CLAHE 1.5 attacks the grain at its source, without a denoiser to soften faint texture
        for clip in (2.0, 1.5)
        for amount in (0.6, 1.0)
    ),
    *(
        Method(
            f"nlm-{h}+espcn-x2+clahe-2.0+edge-{amount}",
            2,
            then(
                nlm(h),
                then(superres("espcn", 2), then(clahe(2.0, 0), edge_sharpen(amount))),
            ),
            "sr-cnn",
            ("ESPCN_x2.pb",),
        )
        for h, amount in [(3, 0.6), (3, 1.0), (5, 1.0)]
    ),
    Method("realesrgan-x4", 4, realesrgan, "sr-gan", ("realesr-general-x4v3.onnx",)),
    *(
        Method(
            f"realesrgan-x4+clahe-{clip}-tiles{tiles}",
            4,
            then(realesrgan, clahe_tiles(clip, tiles)),
            "sr-gan",
            ("realesr-general-x4v3.onnx",),
        )
        for clip in (1.5, 2.0)
        for tiles in (4, 8)
    ),
]

BY_NAME = {m.name: m for m in METHODS}


def pick(names: str) -> list[Method]:
    """ "all", or a comma-separated list of method names."""
    if names == "all":
        return METHODS
    wanted = list(dict.fromkeys(names.split(",")))  # a name given twice is scored once
    unknown = [n for n in wanted if n not in BY_NAME]
    if unknown:
        raise SystemExit(f"unknown method(s) {unknown}; known: {', '.join(BY_NAME)}")
    return [BY_NAME[n] for n in wanted]

# The pipeline's method must be one this registry scored, under the same name.
assert runtime.METHOD in BY_NAME, f"{runtime.METHOD} is not a scored candidate"
