#!/usr/bin/env python3
"""Build the app icon set from assets/icon-source.* (PNG or JPEG).

The background (taken from the corners) becomes transparent:
- black corners around a rounded square: the anti-aliased rim gets its opacity
  from its brightness relative to the solid color just inside it;
- white background with soft glows/shadows: "color to alpha" on the area
  connected to the border, so glows become real semi-transparency (enclosed
  white areas, e.g. inside a ring, stay opaque). The result is cropped to a
  square around the artwork.

    python3 tools/make_app_icon.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from image_tracer import APP_ID  # noqa: E402

SRC = next(os.path.join(ROOT, "assets", f"icon-source.{e}") for e in ("png", "jpg", "jpeg")
           if os.path.exists(os.path.join(ROOT, "assets", f"icon-source.{e}")))
OUT = os.path.join(ROOT, "assets", "icons", "hicolor")
SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)
BLACK = 8    # max channel value counted as background
RIM = 4      # px of anti-aliased rim
OUTER_MAX_ALPHA = 0.5  # white bg: glow pixels lighter than this get color-to-alpha
NEAR_WHITE = 0.08      # white bg: "background" = border-connected pixels lighter than this
GLOW_REACH = 40        # white bg: px from the background where glows are made transparent
CENTER_SHARE = 0.25    # white bg: enclosed white within this share of the artwork size
                       # from its center is design, farther out it is background
MARGIN = 0.03          # white bg: padding around the artwork in the square crop


def _flood_background(dark: np.ndarray) -> np.ndarray:
    """Dark pixels connected to the image border."""
    from gradient_trace import label_components
    labels = label_components(dark)
    border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    ids = np.unique(border[border > 0])
    return np.isin(labels, ids)


def _white_background(light: np.ndarray) -> np.ndarray:
    """Near-white areas that are background: everything touching the border,
    plus enclosed white pockets toward the edges of the artwork. Enclosed
    white near the artwork's center is part of the design (e.g. the glowing
    disc inside a ring, split into pieces by whatever sits on it)."""
    from gradient_trace import label_components
    labels = label_components(light)
    border = set(np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])))
    ys, xs = np.nonzero(~light)
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2  # artwork center
    reach = CENTER_SHARE * max(xs.max() - xs.min(), ys.max() - ys.min())
    bg = light.copy()
    for c in range(1, labels.max() + 1):
        if c in border:
            continue
        cys, cxs = np.nonzero(labels == c)
        if cxs.size and np.hypot(cxs.mean() - cx, cys.mean() - cy) < reach:
            bg[labels == c] = False  # design element, stays opaque
    return bg


def white_to_alpha(img: Image.Image) -> Image.Image:
    rgb = np.asarray(img.convert("RGB")).astype(np.float32)
    # color-to-alpha against white: p = a*f + (1-a)*255
    a = ((255 - rgb).max(-1) / 255).clip(0, 1)
    # Background = near-white area touching the border (enclosed white stays).
    # Glows/shadows next to it get color-to-alpha, but only within GLOW_REACH px,
    # so faint glow inside a nearly closed ring can't leak through its gaps.
    bg = _white_background(a < NEAR_WHITE)
    # Grow only through light pixels, so solid shapes (the ring) act as walls.
    soft = a < OUTER_MAX_ALPHA
    zone = bg
    for _ in range(GLOW_REACH):
        zone = (np.asarray(Image.fromarray(zone.astype(np.uint8) * 255)
                           .filter(ImageFilter.MaxFilter(3))) > 0) & soft
    outer = bg | zone
    alpha = np.where(outer, a, 1.0)
    color = rgb.copy()
    sel = outer & (a > 0.01)
    color[sel] = np.clip(255 - (255 - rgb[sel]) / a[sel, None], 0, 255)
    out = Image.fromarray(np.dstack([color, alpha * 255]).round().astype(np.uint8), "RGBA")
    # square crop around the artwork
    ys, xs = np.nonzero(alpha > 0.04)
    cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
    side = int(max(xs.max() - xs.min(), ys.max() - ys.min()) * (1 + 2 * MARGIN))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.alpha_composite(out, (int(side / 2 - cx), int(side / 2 - cy)))
    return canvas


def transparent_corners(img: Image.Image) -> Image.Image:
    rgb = np.asarray(img.convert("RGB")).astype(np.float32)
    peak = rgb.max(-1)
    bg = _flood_background(peak <= BLACK)
    near_bg = np.asarray(Image.fromarray(bg.astype(np.uint8) * 255)
                         .filter(ImageFilter.MaxFilter(2 * RIM + 1))) > 0
    rim = near_bg & ~bg
    # Reference brightness = the solid color just inside the rim (local, since
    # the background gradient runs from light blue to dark navy).
    inner = np.where(near_bg, 0, peak).astype(np.uint8)
    ref = np.asarray(Image.fromarray(inner).filter(ImageFilter.MaxFilter(4 * RIM + 1)),
                     np.float32)
    alpha = np.ones(peak.shape, np.float32)
    alpha[bg] = 0
    a = np.clip(peak / np.maximum(ref, 1), 0, 1)
    alpha[rim] = a[rim]
    color = rgb.copy()
    unpremul = rim & (alpha > 0.02)
    color[unpremul] = np.clip(rgb[unpremul] / alpha[unpremul, None], 0, 255)
    out = np.dstack([color, alpha * 255]).round().astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def main():
    src = Image.open(SRC).convert("RGB")
    corners = np.asarray(src)[[0, 0, -1, -1], [0, -1, 0, -1]].mean()
    icon = white_to_alpha(src) if corners > 128 else transparent_corners(src)
    for s in SIZES:
        d = os.path.join(OUT, f"{s}x{s}", "apps")
        os.makedirs(d, exist_ok=True)
        icon.resize((s, s), Image.Resampling.LANCZOS).save(os.path.join(d, APP_ID + ".png"))
    print(f"wrote {len(SIZES)} sizes to {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    main()
