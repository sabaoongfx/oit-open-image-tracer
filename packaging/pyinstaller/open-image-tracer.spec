# -*- mode: python -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# PyInstaller spec shared by the AppImage, Windows and macOS builds.
# The build scripts tell it where the native pieces are:
#   POTRACE_LIB   path to libpotrace (.so.0 / -0.dll / .dylib)
#   TESSERACT     path to the tesseract executable (optional: no OCR without it)
#   TESSDATA      folder containing eng.traineddata (needed with TESSERACT)
#   APP_ICON      .ico (Windows) or .icns (macOS) for the executable/app (optional)
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
APP_ID = "io.github.sabaoongfx.OpenImageTracer"
VERSION = open(os.path.join(ROOT, "VERSION")).read().strip()

binaries = [(os.environ["POTRACE_LIB"], ".")]
datas = [
    (os.path.join(ROOT, "assets", "thumbs"), os.path.join("assets", "thumbs")),
    (os.path.join(ROOT, "assets", "icons"), os.path.join("assets", "icons")),
    (os.path.join(ROOT, "assets", "samples", "ui.jpg"), os.path.join("assets", "samples")),
]
if os.environ.get("TESSERACT"):
    binaries.append((os.environ["TESSERACT"], "."))
    datas.append((os.path.join(os.environ["TESSDATA"], "eng.traineddata"), "tessdata"))

a = Analysis(
    [os.path.join(ROOT, "image_tracer.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=["trace_engine", "presets", "centerline"],
    hooksconfig={
        "gi": {
            "icons": ["Adwaita", "hicolor"],
            "themes": ["Adwaita"],
            "languages": ["en_US"],
            "module-versions": {"Gtk": "4.0", "Gdk": "4.0"},
        },
    },
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide6", "matplotlib", "scipy"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="open-image-tracer",
    console=False,
    icon=os.environ.get("APP_ICON") or None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="open-image-tracer")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Image Tracer.app",
        icon=os.environ.get("APP_ICON") or None,
        bundle_identifier=APP_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": "Image Tracer",
            "CFBundleDisplayName": "Image Tracer",
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "Image",
                "CFBundleTypeRole": "Viewer",
                "LSItemContentTypes": ["public.png", "public.jpeg", "com.microsoft.bmp",
                                       "com.compuserve.gif", "public.tiff", "org.webmproject.webp"],
            }],
        },
    )
