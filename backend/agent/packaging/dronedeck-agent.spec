# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the agent sidecar.

Two classes of dependency here that static analysis cannot see on its own, and
each one fails at runtime rather than at build time — which is why they are
listed explicitly instead of being discovered when a drone will not connect.

1. **uvicorn's protocol and loop implementations.** uvicorn selects them by
   string name at startup, so nothing imports them where PyInstaller can see
   it. Left out, the frozen binary starts and then cannot serve a request.

2. **libusb.** cflib reaches the radio through `libusb_package`, which ships a
   native `libusb-1.0.dylib` beside its Python source. That package registers
   its own PyInstaller hook, so the dylib is collected automatically — but it
   is asserted in `packaging/verify_sidecar.py` rather than assumed, because a
   missing dylib is invisible until a radio is plugged in.

Built via `packaging/build_sidecar.py`, which names the output with the target
triple Tauri expects of an `externalBin`.
"""

import os

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    # Selected by name at runtime — see note 1 above.
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.loops.auto",
    "uvicorn.loops.uvloop",
    "uvicorn.loops.asyncio",
    # pyserial picks its port lister per platform at import time.
    *collect_submodules("serial.tools"),
    # cflib registers drivers from its own package at init_drivers() time.
    *collect_submodules("cflib.crtp"),
    *collect_submodules("cflib.drivers"),
]

a = Analysis(
    ["sidecar.py"],
    pathex=[".."],
    binaries=[],
    # The camera firmware Set up installs on a drone (drone_setup.py).
    datas=[(os.path.join(SPECPATH, "..", "dronedeck", "firmware_bundle"),
            "dronedeck/firmware_bundle"),
           # MODELS.json and the display chain's Real-ESRGAN (display/steps.py).
           (os.path.join(SPECPATH, "..", "dronedeck", "pipeline", "models"),
            "dronedeck/pipeline/models")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The notebook stack is an optional extra for analysis and would roughly
    # double the binary. The agent never imports it to fly.
    excludes=["pandas", "matplotlib", "notebook", "IPython", "tkinter"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="dronedeck-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # One file, because the sidecar contract is a single executable path and a
    # user should never see a folder of loose libraries next to the app.
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
