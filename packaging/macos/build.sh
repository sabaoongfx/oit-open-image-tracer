#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build "Image Tracer.app" and a .dmg with Homebrew's GTK 4 stack.
#   packaging/macos/build.sh OUTDIR
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$(mkdir -p "${1:-dist}" && cd "${1:-dist}" && pwd)
VERSION=$(cat "$ROOT/VERSION")
ARCH=$(uname -m)   # arm64 or x86_64
BUILD="$ROOT/build/macos"
rm -rf "$BUILD" && mkdir -p "$BUILD"
BREW=$(brew --prefix)

# Homebrew's python that has PyGObject (pygobject3 is built for it)
PY=""
for p in "$BREW"/opt/python@3.*/bin/python3.*; do
    case "$p" in *-config) continue ;; esac
    if "$p" -c "import gi, numpy, PIL" 2>/dev/null; then PY="$p"; fi
done
[ -n "$PY" ] || { echo "no Homebrew python with gi/numpy/PIL" >&2; exit 1; }
echo "using $PY"
"$PY" -m pip install --break-system-packages --quiet pyinstaller

export POTRACE_LIB=$(ls "$(brew --prefix potrace)"/lib/libpotrace.*.dylib | head -n 1)
export TESSERACT="$(brew --prefix tesseract)/bin/tesseract"
export TESSDATA="$(brew --prefix tesseract)/share/tessdata"
"$PY" "$ROOT/tools/make_platform_icon.py" "$BUILD/app.icns"
export APP_ICON="$BUILD/app.icns"
"$PY" -m PyInstaller --noconfirm --distpath "$BUILD/dist" --workpath "$BUILD/work" \
    "$ROOT/packaging/pyinstaller/open-image-tracer.spec"

# .dmg with the app and an Applications shortcut
STAGE="$BUILD/dmg"
mkdir -p "$STAGE"
cp -R "$BUILD/dist/Image Tracer.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
NAME=$([ "$ARCH" = arm64 ] && echo aarch64 || echo x64)
hdiutil create -volname "Image Tracer" -srcfolder "$STAGE" -ov -format UDZO \
    "$OUT/Open-Image-Tracer-$VERSION-$NAME.dmg"
