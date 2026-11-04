#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Build Open-Image-Tracer-<version>-x86_64.AppImage (run on Ubuntu 24.04 with the
# packages from the release workflow installed).   packaging/appimage/build-appimage.sh OUTDIR
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$(mkdir -p "${1:-dist}" && cd "${1:-dist}" && pwd)
VERSION=$(cat "$ROOT/VERSION")
APP=io.github.sabaoongfx.OpenImageTracer
BUILD="$ROOT/build/appimage"
rm -rf "$BUILD" && mkdir -p "$BUILD"

POTRACE_LIB=$(ldconfig -p | awk '/libpotrace\.so\.0 / {print $NF; exit}')
TESSERACT=$(command -v tesseract)
TESSDATA=$(dirname "$(find /usr/share -name eng.traineddata | head -n 1)")
export POTRACE_LIB TESSERACT TESSDATA
python3 -m PyInstaller --noconfirm --distpath "$BUILD/dist" --workpath "$BUILD/work" \
    "$ROOT/packaging/pyinstaller/open-image-tracer.spec"

APPDIR="$BUILD/AppDir"
mkdir -p "$APPDIR/usr/lib" "$APPDIR/usr/share/metainfo"
cp -a "$BUILD/dist/open-image-tracer" "$APPDIR/usr/lib/"
cat > "$APPDIR/AppRun" <<'APPRUN'
#!/bin/sh
HERE=$(dirname "$(readlink -f "$0")")
exec "$HERE/usr/lib/open-image-tracer/open-image-tracer" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"
cp "$ROOT/packaging/linux/$APP.desktop" "$APPDIR/$APP.desktop"
cp "$ROOT/assets/icons/hicolor/256x256/apps/$APP.png" "$APPDIR/$APP.png"
ln -sf "$APP.png" "$APPDIR/.DirIcon"
cp "$ROOT/packaging/linux/$APP.metainfo.xml" "$APPDIR/usr/share/metainfo/$APP.appdata.xml"

TOOL="$BUILD/appimagetool"
curl -sSfL -o "$TOOL" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
chmod +x "$TOOL"
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 "$TOOL" --no-appstream "$APPDIR" \
    "$OUT/Open-Image-Tracer-$VERSION-x86_64.AppImage"
