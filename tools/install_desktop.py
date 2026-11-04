#!/usr/bin/env python3
"""Install (or remove) the app icon and a launcher entry for the current user.

Puts the icons from assets/icons/hicolor into ~/.local/share/icons/hicolor and
writes ~/.local/share/applications/<APP_ID>.desktop, so the app appears in the
menu and the window/taskbar show its icon (on Wayland the taskbar icon comes
from the .desktop file matching the app id). No root needed.

    python3 tools/install_desktop.py            # install / update
    python3 tools/install_desktop.py --uninstall
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from image_tracer import APP_ID  # noqa: E402

DATA = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
ICONS_SRC = os.path.join(ROOT, "assets", "icons", "hicolor")
ICONS_DST = os.path.join(DATA, "icons", "hicolor")
DESKTOP = os.path.join(DATA, "applications", APP_ID + ".desktop")

ENTRY = f"""[Desktop Entry]
Type=Application
Name=Image Tracer
GenericName=Image to SVG Converter
Comment=Convert images (PNG, JPEG, …) into SVG vectors
Exec={sys.executable} {os.path.join(ROOT, "image_tracer.py")} %f
Icon={APP_ID}
Terminal=false
Categories=Graphics;2DGraphics;VectorGraphics;
MimeType=image/png;image/jpeg;image/bmp;image/gif;image/tiff;image/webp;
StartupNotify=true
StartupWMClass={APP_ID}
"""


def _refresh():
    for cmd in (["gtk-update-icon-cache", "-f", "-t", ICONS_DST],
                ["update-desktop-database", os.path.dirname(DESKTOP)],
                ["kbuildsycoca6"], ["kbuildsycoca5"]):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, capture_output=True)


def install():
    for size in os.listdir(ICONS_SRC):
        src = os.path.join(ICONS_SRC, size, "apps", APP_ID + ".png")
        dst_dir = os.path.join(ICONS_DST, size, "apps")
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(src, dst_dir)
    os.makedirs(os.path.dirname(DESKTOP), exist_ok=True)
    with open(DESKTOP, "w", encoding="utf-8") as fh:
        fh.write(ENTRY)
    _refresh()
    print(f"installed icon ({APP_ID}) and launcher {DESKTOP}")


def uninstall():
    for size in os.listdir(ICONS_SRC):
        p = os.path.join(ICONS_DST, size, "apps", APP_ID + ".png")
        if os.path.exists(p):
            os.remove(p)
    if os.path.exists(DESKTOP):
        os.remove(DESKTOP)
    _refresh()
    print("removed icon and launcher")


if __name__ == "__main__":
    uninstall() if "--uninstall" in sys.argv else install()
