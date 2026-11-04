#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Install Image Tracer into DESTDIR/PREFIX. Shared by the .deb, .rpm, pacman and
# Flatpak builds so they all ship the same files.
#   packaging/linux/install.sh DESTDIR PREFIX      e.g.  pkgroot /usr   or   "" /app
set -eu
DESTDIR=${1:-}
PREFIX=${2:-/usr}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
APP=io.github.sabaoongfx.OpenImageTracer
share="$DESTDIR$PREFIX/share"
lib="$share/open-image-tracer"

install -d "$lib/assets/samples" "$share/icons"
install -m644 "$ROOT/image_tracer.py" "$ROOT/trace_engine.py" "$ROOT/centerline.py" \
    "$ROOT/presets.py" "$lib/"
cp -R "$ROOT/assets/thumbs" "$ROOT/assets/icons" "$lib/assets/"
install -m644 "$ROOT/assets/samples/ui.jpg" "$lib/assets/samples/"   # for --self-test
cp -R "$ROOT/assets/icons/hicolor" "$share/icons/"
install -Dm755 "$ROOT/packaging/linux/open-image-tracer" "$DESTDIR$PREFIX/bin/open-image-tracer"
install -Dm644 "$ROOT/packaging/linux/$APP.desktop" "$share/applications/$APP.desktop"
install -Dm644 "$ROOT/packaging/linux/$APP.metainfo.xml" "$share/metainfo/$APP.metainfo.xml"
install -Dm644 "$ROOT/LICENSE" "$share/licenses/open-image-tracer/LICENSE"
