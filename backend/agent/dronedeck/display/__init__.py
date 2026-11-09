"""Captured frames made easier to look at, after the pipeline — never as evidence.

The team's display chain (2026-11-04): Real-ESRGAN ×4, then CLAHE. It draws
detail the camera did not record (ml/upscaling-eval/RESULTS.txt, REJECTED),
which the team accepts for SHOWING a frame and nothing else: no stage of the
pipeline reads what comes out of here (docs/features/pipeline/enhance.txt).
It is thousands of times slower than clahe@1, so it runs once a flight's
processing is done, in chunks, and never while the drone is in the air.

    steps.py   what is done to one frame — an ordered list of named,
               versioned steps (upscaling, today)
    frames.py  which frames, in what order, and what is already done:
               <session>/upscaled/<seq>.png and upscaled.csv
    queue.py   sessions waiting, one chunk at a time, in a child process
               (`dronedeck upscale`)
"""
