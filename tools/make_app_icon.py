#!/usr/bin/env python3
"""Build the app icon set from assets/icon-source.png.

The source has solid black corners outside the rounded square. They become
transparent; the anti-aliased rim gets its opacity from how bright each pixel
is relative to the solid color just inside it (pixel = a * color + (1-a) * black).

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

SRC = os.path.join(ROOT, "assets", "icon-source.png")
OUT = os.path.join(ROOT, "assets", "icons", "hicolor")
SIZES = (16, 22, 24, 32, 48, 64, 96, 128, 256, 512)
BLACK = 8    # max channel value counted as background
RIM = 4      # px of anti-aliased rim


def _flood_background(dark: np.ndarray) -> np.ndarray:
    """Dark pixels connected to the image border."""
    from gradient_trace import label_components
    labels = label_components(dark)
    border = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    ids = np.unique(border[border > 0])
    return np.isin(labels, ids)


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
    icon = transparent_corners(Image.open(SRC))
    for s in SIZES:
        d = os.path.join(OUT, f"{s}x{s}", "apps")
        os.makedirs(d, exist_ok=True)
        icon.resize((s, s), Image.Resampling.LANCZOS).save(os.path.join(d, APP_ID + ".png"))
    print(f"wrote {len(SIZES)} sizes to {os.path.relpath(OUT, ROOT)}")


if __name__ == "__main__":
    main()
