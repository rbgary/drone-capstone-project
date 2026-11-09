"""The two sheets dpp-enhance.txt asks for.

    python sheet.py --set camera-frames --before-after espcn-x2
        → before-after-<set>.jpg, committed: the PDF's acceptance criterion,
          "before vs. after images … show visibly more detail".

    python sheet.py --set camera-frames --blind espcn-x2,fsrcnn-x2,realesrgan-x4,clahe-unsharp
        → data/runs/<set>/blind-sheet.jpg to show two teammates, and
          blind-key.json beside it (gitignored, so it stays blind until scored).

    python sheet.py --set camera-frames --compare espcn-x2,espcn-x2+clahe-1.5-0.3
        → data/runs/<set>/compare-sheet.jpg: the same layout, names shown — for tuning.

Both enhance the REAL 324×244 frame — what the stage will be given — not a
shrunk copy. Each frame gets two rows, each the whole frame for context (the
zoomed region boxed) and then that region from the original and from each
method:

  DETAIL   the frame's most detailed region — where a method can show more.
  FLAT     its smoothest region that is not clipped to black or white — where
           a method can only ADD: any texture here that the original lacks is
           grain or invented detail. Zooming on detail alone would show every
           method where it looks best and hide where it invents.

Every zoomed panel is enlarged to the same size with bicubic, as a viewer
would show it, so no panel is given away by its pixels being blockier than
the rest. Both regions keep clear of the crosshair.
"""

from __future__ import annotations

import argparse
import json
import random

import cv2
import numpy as np
from common import CROSSHAIR_BOX, FRAME_H, FRAME_W, HERE, RUNS, Item, load_set, shrink
from methods import Method, pick

ROWS = 10
ZOOM_W, ZOOM_H = FRAME_W // 3, FRAME_H // 3  # the region, in original pixels: enlarged ×3
LABEL_H, GAP = 22, 6
WHITE, BLACK = 255, 0


CLIPPED = 5  # grey levels from black or white: nothing can show there, under any method
MAX_CLIPPED_SHARE = 0.05  # a flat window may be at most this much clipped


def window_sums(values: np.ndarray) -> np.ndarray:
    """values summed over every ZOOM_W×ZOOM_H window, indexed by its top-left corner."""
    return cv2.boxFilter(
        values, -1, (ZOOM_W, ZOOM_H), normalize=False, anchor=(0, 0), borderType=cv2.BORDER_CONSTANT
    )[: FRAME_H - ZOOM_H, : FRAME_W - ZOOM_W]


def regions(frame: np.ndarray, mask: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """Top-left corners of the DETAIL and FLAT windows (the module docstring)."""
    energy = np.abs(cv2.Laplacian(frame.astype(np.float32), cv2.CV_32F))
    off_limits = np.zeros_like(energy)
    if mask == "crosshair":
        x0, y0, x1, y1 = CROSSHAIR_BOX
        off_limits[
            int(y0 * FRAME_H) - 2 : int(y1 * FRAME_H) + 3,
            int(x0 * FRAME_W) - 2 : int(x1 * FRAME_W) + 3,
        ] = 1
    clipped = ((frame <= CLIPPED) | (frame >= 255 - CLIPPED)).astype(np.float32)
    detail = window_sums(energy * (1 - off_limits))
    flat = window_sums(energy)
    flat[window_sums(off_limits) > 0] = np.inf
    flat[window_sums(clipped) > MAX_CLIPPED_SHARE * ZOOM_W * ZOOM_H] = np.inf
    dy, dx = np.unravel_index(int(np.argmax(detail)), detail.shape)
    fy, fx = np.unravel_index(int(np.argmin(flat)), flat.shape)
    return (int(dx), int(dy)), (int(fx), int(fy))


def zoom(img: np.ndarray, x: int, y: int) -> np.ndarray:
    """The region at (x, y) in ORIGINAL pixels, from an image of any scale, shown at 324×244."""
    s = img.shape[1] // FRAME_W
    region = img[y * s : (y + ZOOM_H) * s, x * s : (x + ZOOM_W) * s]
    return cv2.resize(region, (FRAME_W, FRAME_H), interpolation=cv2.INTER_CUBIC)


def labelled(img: np.ndarray, text: str) -> np.ndarray:
    bar = np.full((LABEL_H, *img.shape[1:]), WHITE, np.uint8)  # gray or colour
    # shrunk to fit, so a long method name is never cut off at the panel's edge
    scale = 0.45
    while scale > 0.2 and cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)[0][0] > (
        img.shape[1] - 8
    ):
        scale -= 0.01
    cv2.putText(bar, text, (4, LABEL_H - 7), cv2.FONT_HERSHEY_SIMPLEX, scale, BLACK, 1, cv2.LINE_AA)
    return np.vstack([bar, img])


def context(frame: np.ndarray, x: int, y: int) -> np.ndarray:
    """The whole frame, with the zoomed region outlined."""
    out = frame.copy()
    cv2.rectangle(out, (x, y), (x + ZOOM_W - 1, y + ZOOM_H - 1), WHITE, 1)
    return out


def hstack(panels: list[np.ndarray]) -> np.ndarray:
    gap = np.full((panels[0].shape[0], GAP, *panels[0].shape[2:]), WHITE, np.uint8)
    return np.hstack([p if i == 0 else np.hstack([gap, p]) for i, p in enumerate(panels)])


def vstack(rows: list[np.ndarray], title: str) -> np.ndarray:
    width = rows[0].shape[1]
    gap = np.full((GAP * 2, width, *rows[0].shape[2:]), WHITE, np.uint8)
    head = labelled(np.full((0, width, *rows[0].shape[2:]), WHITE, np.uint8), title)
    parts = [head]
    for r in rows:
        parts += [gap, r]
    return np.vstack(parts)


Region = tuple[str, int, int]  # ("DETAIL" | "FLAT", x, y)


def frames_for(items: list[Item]) -> list[tuple[Item, np.ndarray, list[Region]]]:
    out = []
    for item in items[:ROWS]:
        frame = shrink(item.image(), FRAME_W, FRAME_H)
        (dx, dy), (fx, fy) = regions(frame, item.mask)
        out.append((item, frame, [("DETAIL", dx, dy), ("FLAT", fx, fy)]))
    return out


def zoom_rows(
    n: int, frame: np.ndarray, where: list[Region], outputs: list[tuple[np.ndarray, str]]
) -> list[np.ndarray]:
    """A frame's two rows: context, the original's zoom, then each output's zoom, labelled."""
    return [
        hstack(
            [
                labelled(context(frame, x, y), f"{n}. ORIGINAL - {kind} box"),
                labelled(zoom(frame, x, y), f"{n}. ORIGINAL, {kind} zoom"),
            ]
            + [labelled(zoom(out, x, y), f"{label}, {kind} zoom") for out, label in outputs]
        )
        for kind, x, y in where
    ]


def before_after(set_name: str, method: Method) -> None:
    rows = []
    for n, (_item, frame, where) in enumerate(frames_for(load_set(set_name)), start=1):
        rows += zoom_rows(n, frame, where, [(method.fn(frame), f"ENHANCED ({method.name})")])
    sheet = vstack(
        rows,
        f"Before / after - {set_name} - {method.name}. Enhanced images can contain detail "
        "that is not in the scene: texture in a FLAT zoom that the original lacks was added.",
    )
    path = HERE / f"before-after-{set_name}.jpg"
    cv2.imwrite(str(path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    print(f"{path.name}  {sheet.shape[1]}×{sheet.shape[0]}  {path.stat().st_size / 1e3:.0f} kB")


def comparison_rows(
    set_name: str, methods: list[Method], rng: random.Random | None
) -> tuple[list[np.ndarray], dict[str, dict[str, str]]]:
    """Two rows per frame (zoom_rows). With an rng the methods are shuffled per
    frame — the same order in both its rows — and labelled by letter only."""
    letters = "ABCDEFGH"[: len(methods)]
    key: dict[str, dict[str, str]] = {}
    rows = []
    for n, (item, frame, where) in enumerate(frames_for(load_set(set_name)), start=1):
        order = methods[:]
        if rng:
            rng.shuffle(order)
        key[str(n)] = {
            "id": item.id,
            **{letter: m.name for letter, m in zip(letters, order, strict=True)},
        }
        outputs = [
            (m.fn(frame), f"{n}{letter}" if rng else m.name)
            for letter, m in zip(letters, order, strict=True)
        ]
        rows += zoom_rows(n, frame, where, outputs)
    return rows, key


def blind(set_name: str, methods: list[Method], seed: int) -> None:
    rows, key = comparison_rows(set_name, methods, random.Random(seed))
    letters = "ABCDEFGH"[: len(methods)]
    sheet = vstack(
        rows,
        f"Blind comparison - {set_name}. For each row: which of "
        f"{', '.join(letters)} shows the most real detail in the DETAIL zoom? In the FLAT "
        "zoom, does any show texture or detail that is NOT in the original?",
    )
    out_dir = RUNS / set_name
    out_dir.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_dir / "blind-sheet.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])
    (out_dir / "blind-key.json").write_text(
        json.dumps({"seed": seed, "rows": key}, indent=2) + "\n"
    )
    print(
        f"{out_dir / 'blind-sheet.jpg'}  {sheet.shape[1]}×{sheet.shape[0]}  "
        f"(key: {out_dir / 'blind-key.json'} — don't open it until the answers are in)"
    )


def compare(set_name: str, methods: list[Method]) -> None:
    """The blind sheet's layout with the names shown — for tuning, not for judging."""
    rows, _ = comparison_rows(set_name, methods, None)
    sheet = vstack(
        rows,
        f"Comparison - {set_name}. Enhanced images can contain detail that is not in the scene.",
    )
    path = RUNS / set_name / "compare-sheet.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])
    print(f"{path}  {sheet.shape[1]}×{sheet.shape[0]}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--set", required=True)
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--before-after", metavar="METHOD")
    what.add_argument("--blind", metavar="METHODS", help="comma-separated, 2 to 8")
    what.add_argument("--compare", metavar="METHODS", help="as --blind, names shown")
    ap.add_argument("--seed", type=int, default=20261007)
    args = ap.parse_args()

    if args.before_after:
        before_after(args.set, pick(args.before_after)[0])
        return
    methods = pick(args.blind or args.compare)
    if not 1 <= len(methods) <= 8:
        raise SystemExit("--blind and --compare take 1 to 8 methods")
    if args.blind:
        blind(args.set, methods, args.seed)
    else:
        compare(args.set, methods)


if __name__ == "__main__":
    main()
