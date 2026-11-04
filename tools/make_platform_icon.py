#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the app icon as .ico (Windows) or .icns (macOS) from assets/icons.

    python3 tools/make_platform_icon.py out.ico|out.icns
"""
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ID = "io.github.sabaoongfx.OpenImageTracer"
src = Image.open(os.path.join(ROOT, "assets", "icons", "hicolor", "512x512", "apps",
                              APP_ID + ".png")).convert("RGBA")
out = sys.argv[1]
if out.endswith(".ico"):
    src.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
else:
    src.save(out)  # .icns: Pillow writes the standard sizes
print("wrote", out)
