"""Download the candidate models into data/models/, check their hashes, and
export Real-ESRGAN to ONNX — the form the agent would run it in (contract,
MODEL FILES: ONNX, run with ONNX Runtime).

    python fetch_models.py                 # also DDColor's checkpoints and code
    python fetch_models.py --ddcolor-onnx  # and DDColor to ONNX: more than 8 GB of memory

DDColor's ONNX export is asked for, never automatic: colour is a stretch goal
(2026-11-04) that chain.py previews in PyTorch, and the export was killed
for memory on an 8 GB machine (export_ddcolor).

Every model's source and licence is recorded here, and goes into MODELS.json
with the one that wins.
"""

from __future__ import annotations

import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from common import MODELS, sha256


@dataclass(frozen=True)
class Download:
    file: str
    url: str
    sha256: str
    license: str


# OpenCV's dnn_superres docs link these (docs.opencv.org, "Super Resolution
# using Convolutional Neural Networks"). Hashes pinned on 2026-10-07.
DOWNLOADS = [
    Download(
        "FSRCNN_x2.pb",
        "https://github.com/Saafke/FSRCNN_Tensorflow/raw/master/models/FSRCNN_x2.pb",
        "366b33f0084c7b3f2bf6724f0a2c77bca94fcec9d7b6d72389d330073b380d5c",
        "Apache-2.0 (github.com/Saafke/FSRCNN_Tensorflow)",
    ),
    Download(
        "FSRCNN_x4.pb",
        "https://github.com/Saafke/FSRCNN_Tensorflow/raw/master/models/FSRCNN_x4.pb",
        "5c68d18db561aed8ead4ffedf1b897ea615baaf60ebf6c35f8e641f8fa4a21bf",
        "Apache-2.0 (github.com/Saafke/FSRCNN_Tensorflow)",
    ),
    Download(
        "ESPCN_x2.pb",
        "https://github.com/fannymonori/TF-ESPCN/raw/master/export/ESPCN_x2.pb",
        "59f77351e1d7c0057bf6fe088b4a8a07e42c468c8c8aebb674a6b4ea1823221d",
        "Apache-2.0 (github.com/fannymonori/TF-ESPCN)",
    ),
    Download(
        "ESPCN_x4.pb",
        "https://github.com/fannymonori/TF-ESPCN/raw/master/export/ESPCN_x4.pb",
        "e403f06309229cf36009cd8fb0da032ba7643fae9f15cf94fe562e8edf8fef47",
        "Apache-2.0 (github.com/fannymonori/TF-ESPCN)",
    ),
    Download(
        "realesr-general-x4v3.pth",
        "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
        "8dc7edb9ac80ccdc30c3a5dca6616509367f05fbc184ad95b731f05bece96292",
        "BSD-3-Clause (github.com/xinntao/Real-ESRGAN)",
    ),
]

REALESRGAN_ONNX = "realesr-general-x4v3.onnx"

# DDColor-large, the colour step (2026-11-04: the team's pick). Two of the three
# large checkpoints: modelscope is the author's default "especially for images
# outside ImageNet"; artistic was trained without the colourfulness loss and may
# give "fewer unreasonable color artifacts" (MODEL_ZOO.md). The third, paper, only
# reproduces the paper. Hashes are Hugging Face's LFS oids, pinned 2026-11-04.
DDCOLOR = ("modelscope", "artistic")
DDCOLOR_DOWNLOADS = [
    Download(
        "ddcolor_modelscope.pth",
        "https://huggingface.co/piddnad/DDColor-models/resolve/main/ddcolor_modelscope.pth",
        "17c460d7e55b32a598370621d77173be59e03c24b0823f06821db23a50c263ce",
        "Apache-2.0 (huggingface.co/piddnad/DDColor-models)",
    ),
    Download(
        "ddcolor_artistic.pth",
        "https://huggingface.co/piddnad/DDColor-models/resolve/main/ddcolor_artistic.pth",
        "a591ee5beedad36de703d8977e92d0c30c1eedc6453fd1b300f3b460842a23a9",
        "Apache-2.0 (huggingface.co/piddnad/DDColor-models)",
    ),
]
# The architecture is DDColor's own code at a pinned commit: ConvNeXt-L and a
# transformer decoder are too much to restate here the way SRVGGNetCompact is.
# Only the export imports it — the ONNX file is what anything else runs.
DDCOLOR_REPO = "https://github.com/piddnad/DDColor.git"
DDCOLOR_COMMIT = "2adb63f2656ac41cbdf7b894cddd94121a3faf13"
DDCOLOR_SRC = MODELS.parent / "vendor" / "DDColor"
DDCOLOR_SIZE = 512  # the size the model sees, whatever the frame's (ddcolor/pipeline.py)


def ddcolor_onnx(checkpoint: str, fp16: bool = False) -> str:
    return f"ddcolor-{checkpoint}{'-fp16' if fp16 else ''}.onnx"


def fetch(d: Download) -> None:
    path = MODELS / d.file
    if not path.exists():
        print(f"downloading {d.file}")
        urllib.request.urlretrieve(d.url, path)
    got = sha256(path)
    if d.sha256 and got != d.sha256:
        path.unlink()
        raise SystemExit(
            f"{d.file}: sha256 {got} is not the pinned {d.sha256} — deleted; "
            "the upstream file changed, check it before re-pinning"
        )
    print(f"ok  {d.file}  {path.stat().st_size / 1e6:.2f} MB  sha256 {got}")


def export_realesrgan() -> None:
    """realesr-general-x4v3 is an SRVGGNetCompact: 32 convolutions and a pixel
    shuffle. The architecture is defined here rather than installing basicsr,
    whose only use would be this one class."""
    import torch
    from torch import nn
    from torch.nn import functional as F

    class SRVGGNetCompact(nn.Module):
        def __init__(self, num_feat: int = 64, num_conv: int = 32, upscale: int = 4) -> None:
            super().__init__()
            self.upscale = upscale
            body: list[nn.Module] = [
                nn.Conv2d(3, num_feat, 3, 1, 1),
                nn.PReLU(num_parameters=num_feat),
            ]
            for _ in range(num_conv):
                body += [nn.Conv2d(num_feat, num_feat, 3, 1, 1), nn.PReLU(num_parameters=num_feat)]
            body.append(nn.Conv2d(num_feat, 3 * upscale * upscale, 3, 1, 1))
            self.body = nn.ModuleList(body)
            self.upsampler = nn.PixelShuffle(upscale)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            out = x
            for layer in self.body:
                out = layer(out)
            return self.upsampler(out) + F.interpolate(x, scale_factor=self.upscale, mode="nearest")

    out = MODELS / REALESRGAN_ONNX
    if out.exists():
        print(f"ok  {REALESRGAN_ONNX}  {out.stat().st_size / 1e6:.2f} MB  (already exported)")
        return
    state = torch.load(MODELS / "realesr-general-x4v3.pth", map_location="cpu", weights_only=True)
    net = SRVGGNetCompact()
    net.load_state_dict(state.get("params_ema", state.get("params", state)))
    net.eval()
    dummy = torch.rand(1, 3, 61, 81)
    torch.onnx.export(
        net,
        (dummy,),
        str(out),
        input_names=["input"],
        output_names=["output"],
        dynamic_axes={"input": {2: "h", 3: "w"}, "output": {2: "h4", 3: "w4"}},
        opset_version=17,
        dynamo=False,
    )
    # The export must compute what PyTorch computes, or every score below is of the wrong model.
    import numpy as np
    import onnxruntime as ort

    with torch.no_grad():
        want = net(dummy).numpy()
    got = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"]).run(
        None, {"input": dummy.numpy()}
    )[0]
    err = float(np.abs(want - got).max())
    if err > 1e-3:
        out.unlink()
        raise SystemExit(f"ONNX export disagrees with PyTorch by {err} — deleted")
    print(
        f"ok  {REALESRGAN_ONNX}  {out.stat().st_size / 1e6:.2f} MB  "
        f"(max |ONNX − PyTorch| = {err:.1e})"
    )


def ddcolor_source() -> None:
    """DDColor's repository at the pinned commit, under data/vendor/ (gitignored)."""
    import subprocess

    if not DDCOLOR_SRC.exists():
        DDCOLOR_SRC.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", DDCOLOR_REPO, str(DDCOLOR_SRC)], check=True)
    subprocess.run(["git", "-C", str(DDCOLOR_SRC), "checkout", "-q", DDCOLOR_COMMIT], check=True)


def export_ddcolor(checkpoint: str) -> None:
    """DDColor-large to ONNX at its fixed 512×512 input, in fp32 and fp16.

    The fp32 export must compute what PyTorch computes, or every number below is
    of the wrong model. The fp16 copy halves the download; how far its colour
    drifts from fp32 is printed, not judged here — chain.py measures it on real
    frames, where it matters.

    EACH STEP IS A PROCESS OF ITS OWN (ddcolor_step). The model is 912 MB, and
    one process holding the checkpoint, the network, the traced graph and an
    ONNX Runtime session at once went past the 8 GB of the WSL machine it was
    first run on and was killed (2026-11-04). A file is renamed into place only
    once its check passes, so a killed step leaves nothing that looks finished."""
    import subprocess
    import sys

    for step, done in (
        ("export", MODELS / ddcolor_onnx(checkpoint)),
        ("fp16", MODELS / ddcolor_onnx(checkpoint, fp16=True)),
    ):
        if done.exists():
            print(f"ok  {done.name}  {done.stat().st_size / 1e6:.2f} MB  (already exported)")
            continue
        subprocess.run([sys.executable, __file__, "--ddcolor", step, checkpoint], check=True)


def _ddcolor_check(checkpoint: str) -> Path:
    """The input and PyTorch's output on it, kept for the steps after the export."""
    return MODELS / f"ddcolor-{checkpoint}-check.npz"


def ddcolor_step(step: str, checkpoint: str) -> None:
    import gc

    import numpy as np
    import onnxruntime as ort

    out = MODELS / ddcolor_onnx(checkpoint)
    half = MODELS / ddcolor_onnx(checkpoint, fp16=True)

    if step == "export":
        import torch

        sys.path.insert(0, str(DDCOLOR_SRC))
        from ddcolor import DDColor, build_ddcolor_model

        net = build_ddcolor_model(
            DDColor,
            model_path=str(MODELS / f"ddcolor_{checkpoint}.pth"),
            input_size=DDCOLOR_SIZE,
            model_size="large",
            device=torch.device("cpu"),
        )
        gc.collect()  # the checkpoint's own copy of the weights
        # A gray frame as the model is given it: L only, a and b zero, back to
        # RGB — three equal channels, as colour.py feeds it.
        dummy = torch.rand(1, 1, DDCOLOR_SIZE, DDCOLOR_SIZE).repeat(1, 3, 1, 1)
        with torch.inference_mode():
            want = net(dummy).numpy()
        np.savez(_ddcolor_check(checkpoint), input=dummy.numpy(), want=want)
        part = out.with_suffix(".onnx.part")
        torch.onnx.export(
            net,
            (dummy,),
            str(part),
            input_names=["input"],
            output_names=["output"],
            opset_version=17,
            dynamo=False,
        )
        del net
        gc.collect()
        check = np.load(_ddcolor_check(checkpoint))
        got = ort.InferenceSession(str(part), providers=["CPUExecutionProvider"]).run(
            None, {"input": check["input"]}
        )[0]
        err = float(np.abs(check["want"] - got).max())
        if err > 1e-2:  # ab is in Lab units, ±128: a hundredth of a unit is invisible
            part.unlink()
            raise SystemExit(f"{out.name}: ONNX disagrees with PyTorch by {err} — deleted")
        part.rename(out)
        print(f"ok  {out.name}  {out.stat().st_size / 1e6:.2f} MB  (max |ONNX − PyTorch| = {err:.1e})")

    elif step == "fp16":
        import onnx
        from onnxruntime.transformers.float16 import convert_float_to_float16

        part = half.with_suffix(".onnx.part")
        model = convert_float_to_float16(onnx.load(str(out)), keep_io_types=True)
        onnx.save(model, str(part))
        del model
        gc.collect()
        check = np.load(_ddcolor_check(checkpoint))
        got = ort.InferenceSession(str(part), providers=["CPUExecutionProvider"]).run(
            None, {"input": check["input"]}
        )[0]
        diff = np.abs(check["want"] - got)
        part.rename(half)
        print(
            f"ok  {half.name}  {half.stat().st_size / 1e6:.2f} MB  "
            f"(|fp16 − PyTorch| mean {diff.mean():.2f}, max {diff.max():.2f} ab units, on noise)"
        )


def main(ddcolor_onnx_too: bool) -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    for d in DOWNLOADS:
        fetch(d)
    export_realesrgan()
    for d in DDCOLOR_DOWNLOADS:
        fetch(d)
    ddcolor_source()
    if ddcolor_onnx_too:
        for checkpoint in DDCOLOR:
            export_ddcolor(checkpoint)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--ddcolor":
        ddcolor_step(sys.argv[2], sys.argv[3])
    else:
        main(ddcolor_onnx_too="--ddcolor-onnx" in sys.argv[1:])
