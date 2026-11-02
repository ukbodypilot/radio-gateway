#!/usr/bin/env python3
"""PyInstaller entry point for the Radio Gateway Windows client.

The .exe bundles Python and the heavy dependencies (numpy, scipy, sounddevice,
customtkinter, ...) but NOT the client code itself, which changes often. The
code lives as plain .py files in %LOCALAPPDATA%\\RadioGatewayClient\\app, where
the gateway's self-update can overwrite them. This launcher:

  1. seeds that folder from the copies bundled in the exe (first run only),
  2. loads windows_audio_client.py and windows_audio_gui.py from it,
  3. falls back to the bundled copies if the downloaded ones fail to import,
     so a bad update can never leave the client unable to start.

This file is the fixed shell: it is not part of the update bundle, so keep it
tiny. Changes to it need a rebuilt exe (see build_windows_client.ps1).
"""

import importlib.util
import os
import shutil
import sys

MODULES = ("windows_audio_client", "windows_audio_gui")   # load order matters

# Never true at runtime. Gives PyInstaller's import scanner something to follow
# so it collects the dependencies of the modules loaded dynamically below.
if os.environ.get("_WAC_NEVER_SET_"):
    import customtkinter, keyboard, numpy, psutil, pystray, sounddevice  # noqa: F401
    import scipy.signal, soxr  # noqa: F401
    from PIL import Image, ImageDraw  # noqa: F401
    import windows_audio_client, windows_audio_gui  # noqa: F401


def _bundle_dir():
    """Where the exe unpacked its data files (or this folder when run as a script)."""
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _app_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "RadioGatewayClient", "app")
    os.makedirs(path, exist_ok=True)
    return path


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_all(folder):
    for name in MODULES:
        sys.modules.pop(name, None)
    return [_load(n, os.path.join(folder, n + ".py")) for n in MODULES][-1]


def main():
    app = _app_dir()
    bundle = _bundle_dir()
    for name in MODULES:
        dst = os.path.join(app, name + ".py")
        src = os.path.join(bundle, name + ".py")
        if not os.path.isfile(dst) and os.path.isfile(src):
            shutil.copyfile(src, dst)
    try:
        gui = _load_all(app)
    except Exception as e:
        # A downloaded update that won't import: run the bundled known-good copy.
        sys.stderr = sys.stderr or open(os.path.join(app, "..", "client.log"), "a")
        sys.stderr.write(f"[launcher] app dir failed ({type(e).__name__}: {e}); using bundled code\n")
        gui = _load_all(bundle)
    gui.main()


if __name__ == "__main__":
    main()
