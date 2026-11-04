#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Build the Windows bundle and NSIS installer inside MSYS2 UCRT64 (see the release
# workflow for the packages).   packaging/windows/build.sh OUTDIR
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$(mkdir -p "${1:-dist}" && cd "${1:-dist}" && pwd)
VERSION=$(cat "$ROOT/VERSION")
BUILD="$ROOT/build/windows"
rm -rf "$BUILD" && mkdir -p "$BUILD"

export POTRACE_LIB=$(cygpath -w "$(ls /ucrt64/bin/libpotrace-*.dll | head -n 1)")
export TESSERACT=$(cygpath -w /ucrt64/bin/tesseract.exe)
export TESSDATA=$(cygpath -w /ucrt64/share/tessdata)
python "$ROOT/tools/make_platform_icon.py" "$BUILD/app.ico"
export APP_ICON=$(cygpath -w "$BUILD/app.ico")
python -m PyInstaller --noconfirm --distpath "$BUILD/dist" --workpath "$BUILD/work" \
    "$(cygpath -w "$ROOT/packaging/pyinstaller/open-image-tracer.spec")"

makensis -DVERSION="$VERSION" \
    -DSRC="$(cygpath -w "$BUILD/dist/open-image-tracer")" \
    -DICON="$APP_ICON" \
    -DLICENSE="$(cygpath -w "$ROOT/LICENSE")" \
    -DOUTFILE="$(cygpath -w "$OUT/Open-Image-Tracer-$VERSION-x64-setup.exe")" \
    "$(cygpath -w "$ROOT/packaging/windows/installer.nsi")"
