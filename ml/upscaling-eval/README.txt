UPSCALING EVAL — which enhancer the pipeline's stage 2 runs
=========================================================

The evaluation behind docs/handoffs/sprint-1/done/dpp-enhance.txt, steps 1–3:
score each candidate enhancer on real images, look for invented detail, and
pick one by the numbers. RESULTS.txt has the table, the winner and why.

Nothing here runs on the agent. methods.py imports the winner's steps from
espcn_steps.py. The agent does not run them: it ships clahe@1
(docs/features/pipeline/enhance.txt), because ESPCN ×2 tied bicubic here.


SET UP (once)

  cd ml/upscaling-eval
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt --extra-index-url https://download.pytorch.org/whl/cpu
  .venv/bin/python fetch_models.py      # models → data/models/, hashes checked


RUN (per set of images)

  .venv/bin/python prepare.py --src data/originals --name camera-frames --protocol self --mask crosshair
  .venv/bin/python score.py   --set camera-frames
  .venv/bin/python sheet.py   --set camera-frames --before-after espcn-x2+clahe-1.5+edge-0.6
  .venv/bin/python sheet.py   --set camera-frames --blind espcn-x2,espcn-x2+clahe-1.5+edge-0.6,realesrgan-x4
  .venv/bin/python sheet.py   --set camera-frames --compare espcn-x2,espcn-x2+clahe-1.5+edge-0.6

  score.py merges: a run of some methods replaces their rows and keeps the
  rest, so the table always covers every method scored so far.

  When the story 4.1 photos arrive, put them in data/photos/ and run the same
  three with --src data/photos --name photos --protocol reference (no --mask).
  The camera-frames set stays as it is beside it.

  prepare.py takes every image in --src, so keep one folder per set (the
  2026-10-21 screenshots are camera-frames-1021, from their own folder).


WHAT IS COMMITTED, AND WHAT IS NOT

  committed   the scripts; manifests/<set>.csv (each image's name, sha256 and
              crop); before-after-<set>.jpg; RESULTS.txt
  gitignored  data/ — the images, the prepared sets, the models, every run's
              output, and the blind sheet's key. Datasets are never committed
              (dpp-contract.txt, MODEL FILES), and the repository is public.


THE TWO PROTOCOLS (prepare.py says more)

  reference   The image has more detail than the camera: the ticket's protocol.
              The input is the image shrunk to 324×244, the truth is the image
              at the method's output size.
  self        The image IS a camera frame. The truth is the frame; the input is
              the frame shrunk by the method's scale. Used for the 15
              screenshots of 2026-10-06, which are camera frames enlarged
              ~2.56× by a viewer: shrunk to 324×244 and enlarged back they
              return at 42–46 dB, and under 0.6 % of their energy lies above
              the 324×244 Nyquist limit. They carry no detail finer than the
              camera's, so they cannot be the reference.


THE SHEETS — sheet.py's docstring says more

  Each frame gets two zoomed rows: DETAIL, its most detailed region, where a
  method can show more; and FLAT, its smoothest unclipped region, where a
  method can only add. Texture in a FLAT zoom that the original lacks is grain
  or invented detail. --compare is for tuning (names shown); --blind is for
  the teammates' check (letters only, key kept in data/).


THE METRICS — score.py's docstring defines each column

  PSNR / SSIM against the truth, ΔPSNR / ΔSSIM against bicubic at the same
  scale, "edges kept" (sharpness on real edges) and "flat texture" (detail
  where the truth is smooth — the invented-detail check). Read flat texture
  against bicubic's own figure, not against 1.0: the truth's smooth areas are
  nearly noise-free, so even plain interpolation scores 1.2 there.


KNOWN LIMITS

  RESULTS.txt, KNOWN LIMITS — of the method and of this evaluation.
