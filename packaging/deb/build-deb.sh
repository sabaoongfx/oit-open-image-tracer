#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Build an architecture-independent .deb (Debian 13+, Ubuntu 24.04+: needs GTK >= 4.10).
#   packaging/deb/build-deb.sh OUTDIR
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${1:-.}
VERSION=$(cat "$ROOT/VERSION")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

"$ROOT/packaging/linux/install.sh" "$STAGE" /usr
# Debian keeps license texts in /usr/share/doc/<package>/copyright.
install -Dm644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/open-image-tracer/copyright"
rm -rf "$STAGE/usr/share/licenses"

install -d "$STAGE/DEBIAN"
SIZE=$(du -sk "$STAGE/usr" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: open-image-tracer
Version: $VERSION
Section: graphics
Priority: optional
Architecture: all
Maintainer: Sabaoon <sabaoongfx@gmail.com>
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-4.0 (>= 4.10), python3-numpy, python3-pil, libpotrace0, fontconfig
Recommends: tesseract-ocr, tesseract-ocr-eng
Homepage: https://github.com/sabaoongfx/oit-open-image-tracer
Description: Convert images into SVG vectors
 Open Image Tracer turns bitmaps (PNG, JPEG, ...) into scalable SVG vectors, with
 presets for logos, screenshots and line art, OCR for real SVG text, and a
 side-by-side preview.
CONTROL
mkdir -p "$OUT"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT/open-image-tracer_${VERSION}_all.deb"
