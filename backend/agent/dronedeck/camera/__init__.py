"""What is left of the camera: the AI deck's flasher, for Set up.

The drone carries no camera since 2026-11-15 (docs/features/architecture.txt,
DECISIONS): the frame source, the deck's Wi-Fi, the recording and the capture
rule were removed. flash_gap8.py stays only because drone_setup.py still
installs the AI deck's two chips on a drone that has one; it goes with the
deck's parts of Set up, in the no-camera work's firmware phase.
"""
