"""The display chain, timed and checked: Real-ESRGAN ×4 → CLAHE → DDColor-large
(2026-11-04, the team's pick — display only, never evidence).

    python chain.py --set camera-frames                          # on the CPU
    data/venv-gpu/bin/python chain.py --set camera-frames --gpu  # CUDA (README.txt, GPU)
    python chain.py --set camera-frames-1021 --sheet             # + the before/after sheet

Writes data/runs/<set>/chain-<cpu|gpu>.txt (also printed) and, with --sheet,
data/runs/<set>/chain-sheet.jpg.

COLOUR IS A STRETCH GOAL (2026-11-04); the upscale and CLAHE are what must
ship. Each DDColor checkpoint runs from its ONNX export when there is one
(fetch_models.py --ddcolor-onnx, which needs more than 8 GB of memory), and
otherwise as DDColor's own PyTorch network on the CPU — a preview of the
colour, not of the runtime the app would use. One model is loaded at a time.

NOTHING HERE IS SCORED AGAINST A TRUTH. There is no colour truth for a gray
camera, and score.py already scores the upscale. What is measured:

  ms/frame    each step, median, warm, on real 324×244 frames: what the frame
              queue will pay per frame. The colour step's cost does not depend
              on the frame's size — the model always sees 512×512.
  L kept      p99 of |L out − L in| (0..100): the colour step must not move
              brightness. Up to ~0.5 is 8-bit rounding.
  fp16 Δab    mean and p99 of |ab fp16 − ab fp32| on the same frames, in Lab
              units: a ΔE of about 2.3 is the smallest difference people see.
  colourful   Hasler & Süsstrunk's M, mean (colour.py): how strongly coloured,
              not how right.
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import sys
import time
from collections.abc import Callable, Iterator

import cv2
import numpy as np
import onnxruntime as ort
from colour import PredictAb, ab_of, colourfulness, colourise, lightness, onnx_ab, session
from common import FRAME_H, FRAME_W, MODELS, RUNS, load_set, shrink
from fetch_models import DDCOLOR, DDCOLOR_SIZE, DDCOLOR_SRC, REALESRGAN_ONNX, ddcolor_onnx
from methods import bicubic, clahe_tiles, run_realesrgan
from sheet import hstack, labelled, regions, vstack, zoom

# The CLAHE after Real-ESRGAN — score.py's realesrgan-x4+clahe-* rows decide it
# (RESULTS.txt, THE DISPLAY CHAIN): the least flat texture on both sets, edges
# kept like the rest.
CLAHE_CLIP, CLAHE_TILES = 1.5, 4

PANEL_W = 432  # a ×4 frame shown at 4/3 of the camera's width on the sheet


def providers(gpu: bool) -> list[str]:
    if not gpu:
        return ["CPUExecutionProvider"]
    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls()  # the CUDA and cuDNN wheels' libraries (onnxruntime-gpu[cuda,cudnn])
    if "CUDAExecutionProvider" not in ort.get_available_providers():
        raise SystemExit("no CUDA provider: run with data/venv-gpu/bin/python (README.txt, GPU)")
    return ["CUDAExecutionProvider", "CPUExecutionProvider"]


def timed(fn: Callable[[np.ndarray], np.ndarray], inputs: list[np.ndarray]) -> tuple[float, list[np.ndarray]]:
    """Median seconds per input, warm, and every output."""
    fn(inputs[0])  # model load and first-call allocation are not per-frame costs
    outs, times = [], []
    for x in inputs:
        t = time.perf_counter()
        outs.append(fn(x))
        times.append(time.perf_counter() - t)
    return statistics.median(times), outs


def torch_ab(checkpoint: str) -> PredictAb | None:
    """DDColor's PyTorch network on the CPU, or None where PyTorch is not
    installed (data/venv-gpu has none)."""
    try:
        import torch
    except ImportError:
        return None
    sys.path.insert(0, str(DDCOLOR_SRC))
    from ddcolor import DDColor, build_ddcolor_model

    net = build_ddcolor_model(
        DDColor,
        model_path=str(MODELS / f"ddcolor_{checkpoint}.pth"),
        input_size=DDCOLOR_SIZE,
        model_size="large",
        device=torch.device("cpu"),
    )

    def run(x: np.ndarray) -> np.ndarray:
        with torch.inference_mode():
            out: np.ndarray = net(torch.from_numpy(x)).numpy()
        return out

    return run


def colour_models(prov: list[str]) -> Iterator[tuple[str, PredictAb, str]]:
    """(name, model, what it runs as), one at a time: a 912 MB network is
    loaded only once the one before it is let go."""
    for checkpoint in DDCOLOR:
        exported = False
        for fp16 in (False, True):
            path = MODELS / ddcolor_onnx(checkpoint, fp16)
            if path.exists():
                exported = True
                yield path.stem, onnx_ab(session(path, prov)), f"onnx {path.stat().st_size / 1e6:.0f} MB"
        if exported:
            continue
        model = torch_ab(checkpoint) if (MODELS / f"ddcolor_{checkpoint}.pth").exists() else None
        if model is None:
            print(f"left out: ddcolor-{checkpoint} — no ONNX export, and no PyTorch or checkpoint here")
            continue
        yield f"ddcolor-{checkpoint}", model, "pytorch, cpu"


def report(set_name: str, gpu: bool, n: int) -> None:
    prov = providers(gpu)
    frames = [shrink(i.image(), FRAME_W, FRAME_H) for i in load_set(set_name)[:n]]
    up_model = session(MODELS / REALESRGAN_ONNX, prov)

    up_s, ups = timed(lambda f: run_realesrgan(up_model, f), frames)
    clahe = clahe_tiles(CLAHE_CLIP, CLAHE_TILES)
    clahe_s, chained = timed(clahe, ups)
    steps = [
        ("realesrgan-x4", up_s, ""),
        (f"clahe-{CLAHE_CLIP}-tiles{CLAHE_TILES}", clahe_s, ""),
    ]

    coloured: dict[str, list[np.ndarray]] = {}
    for name, model, runs_as in colour_models(prov):
        sec, outs = timed(lambda g, m=model: colourise(m, g), chained)
        coloured[name] = outs
        l_drift = np.concatenate(
            [np.abs(lightness(o) - lightness(g)).ravel() for o, g in zip(outs, chained, strict=True)]
        )
        note = (
            f"L kept p99 {np.percentile(l_drift, 99):.2f} · "
            f"colourful {statistics.fmean(colourfulness(o) for o in outs):.1f} · {runs_as}"
        )
        steps.append((name, sec, note))

    lines = [
        (
            f"set {set_name}: {len(frames)} real 324×244 frames · "
            f"{'GPU ' + gpu_name() if gpu else 'CPU'} · {platform.processor() or platform.machine()}, "
            f"{os.cpu_count()} threads · onnxruntime {ort.__version__} {prov[0]}"
        ),
        "",
        f"{'step':32s} {'ms/frame':>9s}",
    ]
    lines += [f"{name:32s} {sec * 1000:9.1f}  {note}" for name, sec, note in steps]

    for checkpoint in DDCOLOR:
        full, half = coloured.get(f"ddcolor-{checkpoint}"), coloured.get(f"ddcolor-{checkpoint}-fp16")
        if full and half:
            d = np.concatenate(
                [np.hypot(*(ab_of(a) - ab_of(b)).transpose(2, 0, 1)).ravel() for a, b in zip(full, half, strict=True)]
            )
            lines.append(
                f"fp16 Δab {checkpoint:12s} mean {d.mean():.2f} · p99 {np.percentile(d, 99):.2f} "
                "(about 2.3 is just visible)"
            )

    if coloured:
        cheapest = min(s for name, s, _ in steps if name.startswith("ddcolor"))
        total = up_s + clahe_s + cheapest
        lines += [
            "",
            (
                f"chain, fastest colour model: {total * 1000:.0f} ms/frame → "
                f"{total * 1200 / 60:.1f} min for a 10-minute session's 1 200 captured frames"
            ),
        ]

    text = "\n".join(lines)
    out = RUNS / set_name / f"chain-{'gpu' if gpu else 'cpu'}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n")
    print(text)


def gpu_name() -> str:
    try:
        import subprocess

        return subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, check=True,
        ).stdout.strip().splitlines()[0]
    except (OSError, subprocess.CalledProcessError, IndexError):
        return "(unknown)"


def panel(img: np.ndarray) -> np.ndarray:
    bgr = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img
    return cv2.resize(bgr, (PANEL_W, PANEL_W * FRAME_H // FRAME_W), interpolation=cv2.INTER_AREA)


def sheet(set_name: str, gpu: bool, ids: list[str], rows: int) -> None:
    """Per frame: the whole frame through each step, then the most detailed
    region zoomed, where invented detail shows."""
    prov = providers(gpu)
    items = load_set(set_name)
    items = [i for i in items if i.id in ids] if ids else items[:rows]
    up_model = session(MODELS / REALESRGAN_ONNX, prov)
    clahe = clahe_tiles(CLAHE_CLIP, CLAHE_TILES)

    per_frame = []
    for n, item in enumerate(items, start=1):
        frame = shrink(item.image(), FRAME_W, FRAME_H)
        up = run_realesrgan(up_model, frame)
        chained = clahe(up)
        per_frame.append((item, frame, [
            (bicubic(4)(frame), f"{n}. ORIGINAL ({item.id}, enlarged)"),
            (up, "REAL-ESRGAN x4"),
            (chained, f"+ CLAHE {CLAHE_CLIP}, {CLAHE_TILES}x{CLAHE_TILES}"),
        ]))
    for name, model, _ in colour_models(prov):  # every frame per model: one model in memory
        for _, _, outputs in per_frame:
            outputs.append((colourise(model, outputs[2][0]), f"+ COLOUR {name} (a guess)"))

    parts = []
    for item, frame, outputs in per_frame:
        parts.append(hstack([labelled(panel(o), label) for o, label in outputs]))
        (dx, dy), _ = regions(frame, item.mask)
        parts.append(
            hstack([labelled(panel(zoom(o, dx, dy)), f"{label.split(' (')[0]}, detail zoom") for o, label in outputs])
        )

    title = (
        f"Display chain - {set_name}. Every image after the first contains detail and colour "
        "that may not be in the scene: display only, never evidence."
    )
    out = RUNS / set_name / "chain-sheet.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    img = vstack(parts, title)
    cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(f"{out}  {img.shape[1]}×{img.shape[0]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", required=True)
    ap.add_argument("--gpu", action="store_true", help="CUDA, from data/venv-gpu")
    ap.add_argument("--frames", type=int, default=10, help="frames timed (default 10)")
    ap.add_argument("--sheet", action="store_true", help="also write chain-sheet.jpg")
    ap.add_argument("--ids", default="", help="comma-separated frame ids for the sheet")
    ap.add_argument("--rows", type=int, default=6, help="frames on the sheet without --ids")
    args = ap.parse_args()
    report(args.set, args.gpu, args.frames)
    if args.sheet:
        sheet(args.set, args.gpu, [i for i in args.ids.split(",") if i], args.rows)


if __name__ == "__main__":
    main()
