#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Sign built packages and turn them into a signed pacman repo named "open-image-tracer".
# Used by .github/workflows/publish-linux.yml; runnable locally too.
#
#   make-repo.sh <dir containing *.pkg.tar.zst> <output dir>
#
# The signing key is imported from $PACKAGES_GPG_PRIVATE_KEY if set, else the
# keyring's default secret key is used; set GPGKEY=<fingerprint> to choose one
# (repo-add honours it too) when the keyring holds several.
set -euo pipefail
src=$1
out=$2

if [[ -n "${PACKAGES_GPG_PRIVATE_KEY:-}" ]]; then
  echo "$PACKAGES_GPG_PRIVATE_KEY" | gpg --batch --import
fi

mkdir -p "$out"
cp "$src"/*.pkg.tar.zst "$out"/
cd "$out"
for pkg in *.pkg.tar.zst; do
  gpg --batch --yes --detach-sign --no-armor ${GPGKEY:+--local-user "$GPGKEY"} "$pkg"
done
repo-add --sign --include-sigs open-image-tracer.db.tar.gz ./*.pkg.tar.zst

# repo-add makes open-image-tracer.db/.files (and their .sig) symlinks to the .tar.gz
# files; GitHub Pages doesn't serve symlinks, so replace them with real copies.
find . -maxdepth 1 -type l -print0 | while IFS= read -r -d '' link; do
  cp --remove-destination "$(readlink -f "$link")" "$link"
done
