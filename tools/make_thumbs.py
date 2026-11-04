#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the predefined example thumbnails shown on the Mode and Preset
buttons. Draws small sample images, traces them with our own engine and
renders the SVG results to PNG (2x for HiDPI).

    python3 tools/make_thumbs.py
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import presets as P  # noqa: E402
import trace_engine as te  # noqa: E402

SAMPLES = os.path.join(ROOT, "assets", "samples")
THUMBS = os.path.join(ROOT, "assets", "thumbs")
W, H = 320, 220  # sample size
TW, TH = 224, 152  # thumbnail pixels (shown at 112x76)
K = 4  # supersampling for anti-aliased samples


def font(size):
    for f in ("/usr/share/fonts/Adwaita/AdwaitaSans-Regular.ttf",
              "/usr/share/fonts/noto/NotoSans-Regular.ttf",
              "/usr/share/fonts/TTF/DejaVuSans.ttf"):
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def cartoon() -> Image.Image:
    """Flat-color illustration with black outlines (used for every mode)."""
    im = Image.new("RGB", (W * K, H * K), "#9fd8ff")
    d = ImageDraw.Draw(im)
    o = 3 * K
    s = lambda *v: [x * K for x in v]  # noqa: E731
    d.ellipse(s(225, 18, 285, 78), fill="#ffd23f", outline="black", width=o)
    d.ellipse(s(-60, 150, 380, 330), fill="#6cc04a", outline="black", width=o)
    d.rectangle(s(70, 105, 170, 185), fill="#e94f37", outline="black", width=o)
    d.polygon(s(58, 108, 120, 58, 182, 108), fill="#5b3a29", outline="black", width=o)
    d.rectangle(s(105, 140, 135, 185), fill="#3d2b1f", outline="black", width=o)
    d.rectangle(s(80, 118, 100, 136), fill="#cdeeff", outline="black", width=o)
    d.rectangle(s(140, 118, 160, 136), fill="#cdeeff", outline="black", width=o)
    d.rectangle(s(236, 120, 250, 170), fill="#8b5a2b", outline="black", width=o)
    d.ellipse(s(212, 78, 274, 136), fill="#2e9e4f", outline="black", width=o)
    return im.resize((W, H), Image.LANCZOS)


def logo() -> Image.Image:
    im = Image.new("RGBA", (W * K, H * K), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = lambda *v: [x * K for x in v]  # noqa: E731
    d.rounded_rectangle(s(85, 35, 235, 185), radius=34 * K, fill="#2d6cdf")
    d.ellipse(s(180, 45, 225, 90), fill="#ffb400")
    bolt = [(170, 50), (118, 120), (152, 120), (138, 172), (200, 96), (164, 96), (182, 50)]
    d.polygon([(x * K, y * K) for x, y in bolt], fill="white")
    return im.resize((W, H), Image.LANCZOS)


def ui() -> Image.Image:
    """Small dark app window with text (saved as JPEG, like a real screenshot)."""
    k = 2
    im = Image.new("RGB", (W * k, H * k), "#191919")
    d = ImageDraw.Draw(im)
    f, fb = font(12 * k), font(15 * k)
    d.rectangle((0, 0, W * k, 26 * k), fill="#202020")
    d.text((12 * k, 6 * k), "Task Manager", font=f, fill="#ffffff")
    d.rectangle((0, 26 * k, 40 * k, H * k), fill="#1f2533")
    for i in range(5):
        d.rounded_rectangle((12 * k, (40 + 30 * i) * k, 28 * k, (56 + 30 * i) * k),
                            radius=3 * k, outline="#cfd6e0", width=2 * k)
    d.text((54 * k, 36 * k), "App history", font=fb, fill="#ffffff")
    d.text((54 * k, 62 * k), "Delete usage history", font=f, fill="#60cdff")
    rows = [("Notifier", "0.1 MB"), ("Updater", "0 MB"), ("Service", "13.5 MB"),
            ("Helper", "0 MB"), ("Sync", "0.1 MB")]
    for i, (name, val) in enumerate(rows):
        y = (88 + 25 * i) * k
        if i == 0:
            d.rectangle((44 * k, y - 4 * k, W * k, y + 19 * k), fill="#2b2b2b")
        d.rectangle((210 * k, y - 4 * k, W * k, y + 19 * k),
                    fill="#0b2d5b" if i % 2 == 0 else "#122136")
        d.rectangle((56 * k, y, 70 * k, y + 14 * k), fill="#e93233" if i % 2 else "#2d6cdf")
        d.text((78 * k, y), name, font=f, fill="#ffffff")
        d.text((250 * k, y), val, font=f, fill="#ffffff")
    return im.resize((W, H), Image.LANCZOS)


def sketch() -> Image.Image:
    """Pen line art: a cat face and a scribble."""
    im = Image.new("L", (W * K, H * K), 255)
    d = ImageDraw.Draw(im)
    w = 4 * K
    s = lambda *v: [x * K for x in v]  # noqa: E731
    d.ellipse(s(60, 60, 180, 170), outline=0, width=w)
    d.line(s(72, 85, 78, 30, 115, 62), fill=0, width=w, joint="curve")
    d.line(s(125, 62, 162, 30, 168, 85), fill=0, width=w, joint="curve")
    d.ellipse(s(92, 98, 104, 110), fill=0)
    d.ellipse(s(136, 98, 148, 110), fill=0)
    d.line(s(112, 128, 120, 134, 128, 128), fill=0, width=w, joint="curve")
    for dy in (-6, 4):
        d.line(s(100, 130 + dy, 40, 122 + dy * 2), fill=0, width=w)
        d.line(s(140, 130 + dy, 200, 122 + dy * 2), fill=0, width=w)
    pts = [(250 + (8 + i * 0.9) * math.cos(i / 5), 110 + (8 + i * 0.9) * math.sin(i / 5))
           for i in range(70)]
    d.line([(x * K, y * K) for x, y in pts], fill=0, width=w, joint="curve")
    return im.resize((W, H), Image.LANCZOS).convert("RGB")


def render(result: te.TraceResult, name: str):
    tmp = tempfile.NamedTemporaryFile(suffix=".svg", delete=False)
    tmp.write(result.to_svg().encode())
    tmp.close()
    out = os.path.join(THUMBS, name + ".png")
    subprocess.run(["rsvg-convert", "-w", str(TW), "-h", str(TH), "--keep-aspect-ratio",
                    "-b", "white", tmp.name, "-o", out], check=True)
    os.unlink(tmp.name)
    print("wrote", os.path.relpath(out, ROOT))


def save_sample(img, name):
    """Save a sample (JPEG for screenshots, so it has real compression noise)
    and reload it the way the app would."""
    p = os.path.join(SAMPLES, name)
    img.save(p, **({"quality": 82} if name.endswith(".jpg") else {}))
    return te.load_image(p)


# Per-mode settings that make the shared cartoon sample look its best.
MODE_SAMPLE_OPTIONS = {
    "multi_colors": dict(scans=8),
    "brightness": dict(threshold=0.45),
    "centerline": dict(threshold=0.1),  # only the black outlines count as ink
    "edge": dict(threshold=0.3),
    "multi_grays": dict(scans=6),
    "multi_brightness": dict(scans=4),
    "quant": dict(quant_colors=6),
}
# Per-sample overrides for preset thumbnails.
PRESET_SAMPLE_OPTIONS = {"sketch": dict(threshold=0.5)}


def main():
    os.makedirs(SAMPLES, exist_ok=True)
    os.makedirs(THUMBS, exist_ok=True)
    cartoon_img = save_sample(cartoon(), "cartoon.png")
    samples = {
        "logo": save_sample(logo(), "logo.png"),
        "ui": save_sample(ui(), "ui.jpg"),
        "sketch": save_sample(sketch(), "sketch.png"),
    }

    for m in P.MODES:
        r = te.trace(cartoon_img, te.TraceOptions(mode=m.id, **MODE_SAMPLE_OPTIONS.get(m.id, {})))
        render(r, f"mode_{m.id}")

    for p in P.PRESETS:
        opts = te.TraceOptions(**{**p.options, **PRESET_SAMPLE_OPTIONS.get(p.sample, {})})
        render(te.trace(samples[p.sample], opts), f"preset_{p.slug}")
        if p.options.get("ocr"):
            badge(os.path.join(THUMBS, f"preset_{p.slug}.png"), "Aa")


def badge(path, text):
    """Mark a thumbnail with a small corner badge (e.g. 'Aa' = real text)."""
    im = Image.open(path).convert("RGB")
    d = ImageDraw.Draw(im)
    f = font(30)
    tw = d.textlength(text, font=f)
    x1, y1 = im.width - 8, im.height - 8
    x0, y0 = x1 - tw - 20, y1 - 44
    d.rounded_rectangle((x0, y0, x1, y1), radius=10, fill="#ffb400")
    d.text((x0 + 10, y0 + 4), text, font=f, fill="#1b1b1b")
    im.save(path)


if __name__ == "__main__":
    main()
