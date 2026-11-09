"""The colour step: DDColor-large through ONNX Runtime, on a gray frame of any
size — the ×4 frame the display chain hands it (chain.py). Runtime-legal like
methods.py: numpy, OpenCV, ONNX Runtime.

It is DDColor's own ColorizationPipeline (ddcolor/pipeline.py at
fetch_models.DDCOLOR_COMMIT) restated in numpy: the frame shrunk to 512×512,
its L channel given to the model as three equal channels, the model's a and b
enlarged back to the frame and joined to the frame's OWN L.

THE FRAME'S BRIGHTNESS IS KEPT. The model predicts only a and b; L comes from
the input, untouched. chain.py checks it, because a colour step that moved
brightness would be editing the picture, not tinting it.

ONE DELIBERATE DIFFERENCE: a and b are enlarged bilinearly, where the reference
uses nearest. From 512 to the ×4 frame's 1296×976 nearest paints colour in
2.5-pixel blocks; colour carries no fine detail for bilinear to blur.

THE COLOUR IS A GUESS. The camera records none (enhance.txt, "NO COLOUR"):
what comes out is what ImageNet photos usually look like, never what was there.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

SIZE = 512  # fetch_models.DDCOLOR_SIZE: the export's fixed input

#: The model itself: (1, 3, 512, 512) float32 in, (1, 2, 512, 512) a and b out.
#: An ONNX session (onnx_ab) is the runtime form; chain.py also passes
#: DDColor's PyTorch network, while colour is a stretch goal (RESULTS.txt).
PredictAb = Callable[[np.ndarray], np.ndarray]


def session(path: Path, providers: list[str]) -> ort.InferenceSession:
    return ort.InferenceSession(str(path), providers=providers)


def onnx_ab(model: ort.InferenceSession) -> PredictAb:
    return lambda x: model.run(None, {"input": x})[0]


def colourise(predict_ab: PredictAb, gray: np.ndarray) -> np.ndarray:
    """An 8-bit grayscale frame in; the same frame in colour (BGR, 8-bit) out."""
    h, w = gray.shape
    img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR).astype(np.float32) / 255.0
    own_l = cv2.cvtColor(img, cv2.COLOR_BGR2Lab)[:, :, :1]

    small = cv2.resize(img, (SIZE, SIZE))
    l_small = cv2.cvtColor(small, cv2.COLOR_BGR2Lab)[:, :, :1]
    zeros = np.zeros_like(l_small)
    given = cv2.cvtColor(np.concatenate((l_small, zeros, zeros), axis=-1), cv2.COLOR_LAB2RGB)

    ab = predict_ab(np.ascontiguousarray(given.transpose(2, 0, 1)[None]))[0]
    ab = cv2.resize(ab.transpose(1, 2, 0), (w, h), interpolation=cv2.INTER_LINEAR)

    bgr = cv2.cvtColor(np.concatenate((own_l, ab), axis=-1), cv2.COLOR_LAB2BGR)
    out: np.ndarray = np.clip(bgr * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return out


def lightness(bgr: np.ndarray) -> np.ndarray:
    """L, 0..100, of an 8-bit BGR or gray image."""
    if bgr.ndim == 2:
        bgr = cv2.cvtColor(bgr, cv2.COLOR_GRAY2BGR)
    lab: np.ndarray = cv2.cvtColor(bgr.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)[:, :, 0]
    return lab


def ab_of(bgr: np.ndarray) -> np.ndarray:
    lab: np.ndarray = cv2.cvtColor(bgr.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)[:, :, 1:]
    return lab


def colourfulness(bgr: np.ndarray) -> float:
    """Hasler & Süsstrunk's M (2003): 0 is gray; ~33 'moderately', ~59 'quite'
    colourful. How strongly coloured — never how right."""
    b, g, r = (c.astype(np.float64) for c in cv2.split(bgr))
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.hypot(rg.std(), yb.std()) + 0.3 * np.hypot(rg.mean(), yb.mean()))
