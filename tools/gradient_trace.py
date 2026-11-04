#!/usr/bin/env python3
"""Gradient-aware vectorizer for logos and illustrations.

Instead of slicing an image into flat color bands, it finds regions bounded by
sharp edges, fits a color gradient to each region and fills it with an SVG
<linearGradient>. Semi-transparent glows become a blurred shape.

    python3 tools/gradient_trace.py input.png output.svg
"""
from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import trace_engine as te  # noqa: E402

EDGE = 3.5           # Lab change per pixel that counts as a hard edge
MIN_REGION = 40      # px; smaller pieces are merged into a neighbour
COARSE = int(os.environ.get("COARSE", "1"))  # also detect edges at 1/COARSE resolution (soft borders); 1 = off
GLOW_RIM = 9         # px (odd) next to a glow where glow-colored solid pixels join it
GLOW_COLOR_DIST = 35.0  # RGB distance to the glow color for that
SOFT_MIN_REGION = 4000   # px; regions this big get soft-shape refinement
SOFT_LEVELS = (0.035, 0.09)  # luminance deviation from the region's gradient (0..1)
SOFT_MIN_BLOB = 400      # px; smallest soft shape
SOFT_BLUR = 10.0         # px blur of soft shapes
SOFT_DEV_BLUR = 12       # px smoothing of the deviation map before outlining
SOFT_TRACE = te.TraceOptions(speckles=50, smooth_corners=1.33, optimize=2.0)
SHADOW_TOUCH = 0.15      # share of a soft shape's rim on other objects -> shadow
MERGE_EDGE = float(os.environ.get("MERGE_EDGE", "0"))  # >0: merge regions with weak borders
SOFT_MODE = os.environ.get("SOFT_MODE", "off")  # "all" | "shadows" | "off"
GLOW_OBJECT_DIST = 80.0  # semi-transparent pixels this far from the glow color are objects
DETAIL_DIST = 90.0   # RGB distance from the region's colors that marks a lost detail
DETAIL_MIN = 6       # px; smallest recovered detail
DETAIL_MAX_WIDTH = 9  # px (odd); thicker off-gradient areas are shading, not details
FLAT_RANGE = 10.0    # RGB change across a region below which it is flat
SOLID_ALPHA = 128    # shape coverage threshold (anti-aliased edges are half-covered)
GLOW_BAND = 7        # px; semi-transparent bands at least this wide are glows,
                     # thinner ones are just anti-aliased edges
GLOW_BLUR = 10.0     # px blur for the reconstructed glow
N_STOPS = 6
GLOW_REACH = 25      # px (odd); solid pixels this close to a glow get blurred too


def _filter(mask: np.ndarray, flt) -> np.ndarray:
    return np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).filter(flt)) > 0


def label_components(mask: np.ndarray) -> np.ndarray:
    """4-connected component labels (0 = background), run-based union-find."""
    H, W = mask.shape
    labels = np.zeros((H, W), np.int32)
    parent = [0]

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    prev = []
    for y in range(H):
        d = np.diff(np.concatenate(([0], mask[y].astype(np.int8), [0])))
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        cur, j = [], 0
        for s, e in zip(starts, ends):
            lab = 0
            while j < len(prev) and prev[j][1] <= s:
                j += 1
            k = j
            while k < len(prev) and prev[k][0] < e:
                pl = find(prev[k][2])
                if lab == 0:
                    lab = pl
                elif pl != lab:
                    lo, hi = min(pl, lab), max(pl, lab)
                    parent[hi] = lo
                    lab = lo
                k += 1
            if lab == 0:
                lab = len(parent)
                parent.append(lab)
            cur.append((s, e, lab))
            labels[y, s:e] = lab
        prev = cur
    roots = np.array([find(i) for i in range(len(parent))], np.int32)
    _, compact = np.unique(roots, return_inverse=True)  # 0 stays 0
    return compact[labels].astype(np.int32)


def edge_map(rgb: np.ndarray) -> np.ndarray:
    lab = te._srgb_to_lab(rgb)
    mag = np.zeros(rgb.shape[:2], np.float32)
    for c in range(3):
        g = lab[..., c]
        gx = np.zeros_like(g)
        gy = np.zeros_like(g)
        gx[1:-1, 1:-1] = (g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:]
                          - g[:-2, :-2] - 2 * g[1:-1, :-2] - g[2:, :-2])
        gy[1:-1, 1:-1] = (g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:]
                          - g[:-2, :-2] - 2 * g[:-2, 1:-1] - g[:-2, 2:])
        mag += gx * gx + gy * gy
    return np.sqrt(mag) / 4.0  # ~ delta-E per pixel


def _merge_weak(lab, mag):
    """Merge neighbouring regions whose shared border is only a gentle ramp
    (mean edge strength < MERGE_EDGE), e.g. a soft shadow into the background
    it fades into. Sharp object borders stay."""
    n = lab.max() + 1
    sums, counts = np.zeros((n, n)), np.zeros((n, n))
    for a, b, m in ((lab[:, :-1], lab[:, 1:], np.maximum(mag[:, :-1], mag[:, 1:])),
                    (lab[:-1], lab[1:], np.maximum(mag[:-1], mag[1:]))):
        sel = (a != b) & (a >= 0) & (b >= 0)
        lo, hi = np.minimum(a[sel], b[sel]), np.maximum(a[sel], b[sel])
        np.add.at(sums, (lo, hi), m[sel])
        np.add.at(counts, (lo, hi), 1)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for lo, hi in zip(*np.nonzero(counts >= 20)):
        if sums[lo, hi] / counts[lo, hi] < MERGE_EDGE:
            ra, rb = find(lo), find(hi)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    roots = np.array([find(i) for i in range(n)])
    _, compact = np.unique(roots, return_inverse=True)
    return np.where(lab >= 0, compact[np.maximum(lab, 0)], lab).astype(np.int32)


def _recover_details(lab, rgb, solid):
    """Thin details (hairlines, small dots) consist only of edge pixels, so they
    get absorbed into the surrounding region. Pixels whose color is far from
    their region's color model become new regions of their own."""
    pred = np.zeros(rgb.shape, np.float32)
    for k in range(lab.max() + 1):
        ys, xs = np.nonzero(lab == k)
        if ys.size == 0:
            continue
        X = np.stack([np.ones_like(xs), xs, ys], 1).astype(np.float64)
        coef, *_ = np.linalg.lstsq(X, rgb[ys, xs].astype(np.float64), rcond=None)
        pred[ys, xs] = X @ coef
    off = solid & (np.sqrt(((rgb - pred) ** 2).sum(-1)) > DETAIL_DIST)
    comps = label_components(off)
    sizes = np.bincount(comps.ravel())
    # A real detail has some thickness; 1-px slivers along region borders
    # (anti-aliasing between two colors) vanish under a 3x3 erosion.
    thick = np.bincount(comps[_filter(off, ImageFilter.MinFilter(3))], minlength=len(sizes))
    # ... and is thin: wide areas off the gradient are shading (soft shadows),
    # which a sharp-edged flat region would turn into a hard band.
    wide = np.bincount(comps[_filter(off, ImageFilter.MinFilter(DETAIL_MAX_WIDTH))],
                       minlength=len(sizes))
    nxt = lab.max() + 1
    for c in np.flatnonzero((sizes >= DETAIL_MIN) & (thick >= 3) & (wide == 0)):
        if c == 0:
            continue
        lab[comps == c] = nxt
        nxt += 1
    return lab


def fill_colors(fill, xs, ys) -> np.ndarray:
    """Colors a fill produces at the given pixels (N x 3)."""
    if fill[0] == "flat":
        return np.tile(np.array(fill[1], np.float32), (len(xs), 1))
    _, p0, p1, stops, _gid = fill
    d = np.array([p1[0] - p0[0], p1[1] - p0[1]], np.float64)
    L2 = max(float(d @ d), 1e-9)
    t = np.clip(((xs - p0[0]) * d[0] + (ys - p0[1]) * d[1]) / L2, 0, 1)
    offs = np.array([o for o, _ in stops])
    cols = np.array([c for _, c in stops], np.float64)
    return np.stack([np.interp(t, offs, cols[:, c]) for c in range(3)], 1).astype(np.float32)


def soft_shapes(rgb, own, fill, k, clip_id, opts, defs, shadows_only=False):
    """Soft light/dark areas inside a big region that its single gradient
    can't show (background swooshes, drop shadows, fades): traced where the
    image deviates from the region's fill, given their own gradient, blurred
    and clipped to the region."""
    ys, xs = np.nonzero(own)
    base = np.zeros(own.shape, np.float32)
    lum_px = te._luminance(rgb[ys, xs])
    base[ys, xs] = lum_px - te._luminance(fill_colors(fill, xs, ys))
    # Blur in 8 bits (Pillow can't blur float images): 128 = no deviation.
    enc = np.clip(np.round(base * 255 + 128), 0, 255).astype(np.uint8)
    dev = (np.asarray(Image.fromarray(enc).filter(ImageFilter.GaussianBlur(SOFT_DEV_BLUR)),
                      np.float32)
           - 128) / 255
    out = []
    for sign in (-1, 1):
        for level in SOFT_LEVELS:
            blobs = own & (sign * dev > level)
            comps = label_components(blobs)
            sizes = np.bincount(comps.ravel())
            for c in np.flatnonzero(sizes >= SOFT_MIN_BLOB):
                if c == 0:
                    continue
                m = comps == c
                if shadows_only:
                    # Shadows/fades hug the object that casts them; free-floating
                    # background patches are skipped.
                    ring = _filter(m, ImageFilter.MaxFilter(7)) & ~m
                    if (ring & ~own).sum() < SHADOW_TOUCH * max(ring.sum(), 1):
                        continue
                paths = te._trace_mask(m, SOFT_TRACE, 1)  # smooth curves: they get blurred anyway
                if not paths:
                    continue
                f = fit_fill(rgb, m, f"{k}s{sign}{int(level * 1000)}_{c}")
                out.append(_path_with_fill(f, te.Layer((0, 0, 0), paths).svg_d(), defs,
                                           f' filter="url(#soft)" clip-path="url(#{clip_id})"'))
    if out and not any('id="soft"' in d for d in defs):
        defs.append('<filter id="soft" x="-20%" y="-20%" width="140%" height="140%">'
                    f'<feGaussianBlur stdDeviation="{SOFT_BLUR}"/></filter>')
    return out


def _path_with_fill(fill, d, defs, extra=""):
    if fill[0] == "flat":
        return f'<path fill="{te._hex(fill[1])}"{extra} d="{d}"/>'
    _, p0, p1, stops, gid = fill
    st = "".join(f'<stop offset="{o:.3f}" stop-color="{te._hex(c)}"/>' for o, c in stops)
    defs.append(f'<linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
                f'x1="{p0[0]:.1f}" y1="{p0[1]:.1f}" x2="{p1[0]:.1f}" y2="{p1[1]:.1f}">'
                f'{st}</linearGradient>')
    return f'<path fill="url(#{gid})"{extra} d="{d}"/>'


def fit_fill(rgb, mask, region_id):
    """Flat color or a linear gradient (with stops sampled along its axis)."""
    ys, xs = np.nonzero(mask)
    px = rgb[ys, xs].astype(np.float64)
    if len(px) < 30:
        return ("flat", tuple(int(v) for v in np.median(px, 0)))
    X = np.stack([np.ones_like(xs), xs, ys], 1).astype(np.float64)
    coef, *_ = np.linalg.lstsq(X, px, rcond=None)
    B = coef[1:]  # 2x3: color change per pixel in x / y
    u, s, _ = np.linalg.svd(B, full_matrices=False)
    d = u[:, 0]  # direction of fastest color change
    t = xs * d[0] + ys * d[1]
    t0, t1 = np.percentile(t, 1), np.percentile(t, 99)
    if s[0] * (t1 - t0) < FLAT_RANGE or t1 - t0 < 4:
        return ("flat", tuple(int(v) for v in np.median(px, 0)))
    edges = np.linspace(t0, t1, N_STOPS + 1)
    stops = []
    for i in range(N_STOPS):
        sel = (t >= edges[i]) & (t <= edges[i + 1])
        if sel.sum() < 3:
            continue
        stops.append(((edges[i] + edges[i + 1]) / 2, np.median(px[sel], 0)))
    if len(stops) < 2:
        return ("flat", tuple(int(v) for v in np.median(px, 0)))
    tc0, tc1 = stops[0][0], stops[-1][0]
    cx, cy = xs.mean(), ys.mean()
    tc = cx * d[0] + cy * d[1]
    p0 = (cx + (tc0 - tc) * d[0], cy + (tc0 - tc) * d[1])
    p1 = (cx + (tc1 - tc) * d[0], cy + (tc1 - tc) * d[1])
    stops = [((tt - tc0) / max(tc1 - tc0, 1e-9), tuple(int(round(v)) for v in c))
             for tt, c in stops]
    return ("linear", p0, p1, stops, f"g{region_id}")


def vectorize(path: str) -> str:
    img = Image.open(path).convert("RGBA")
    W, H = img.size
    arr = np.asarray(img)
    alpha = arr[..., 3]
    rgb = te._bilateral(np.ascontiguousarray(arr[..., :3]), 0.6)
    semi = (alpha >= 10) & (alpha < 250)
    core = _filter(semi, ImageFilter.MinFilter(GLOW_BAND))  # survives only in wide bands
    glow = semi & _filter(core, ImageFilter.MaxFilter(GLOW_BAND + 2))
    solid = (alpha >= SOLID_ALPHA) & ~glow
    glow_color = None
    if glow.any():
        # The glow's inner edge is nearly opaque but still glow-colored; left in
        # `solid` it would be merged into the object and fray its outline.
        glow_color = np.median(rgb[glow & (alpha >= 100)], 0) if (glow & (alpha >= 100)).any() \
            else np.median(rgb[glow], 0)
        rim = solid & _filter(glow, ImageFilter.MaxFilter(GLOW_RIM))
        like_glow = np.sqrt(((rgb.astype(np.float32) - glow_color) ** 2).sum(-1)) < GLOW_COLOR_DIST
        # Objects reaching into the glow can be partly transparent too (e.g. an arc
        # passing behind the glowing card); their strong own color tells them apart.
        dist = np.sqrt(((rgb.astype(np.float32) - glow_color) ** 2).sum(-1))
        objects = glow & (alpha >= SOLID_ALPHA) & (dist > GLOW_OBJECT_DIST)
        glow &= ~objects
        solid |= objects
        glow |= rim & like_glow
        solid &= ~(rim & like_glow)

    mag = edge_map(rgb)
    edges = mag > EDGE
    if COARSE > 1:
        # Soft-but-definite borders (a ramp over several px, e.g. background
        # swooshes) are gentle per pixel but become steps at lower resolution;
        # smooth gradients stay gentle there.
        small = np.asarray(Image.fromarray(rgb).resize((W // COARSE, H // COARSE),
                                                       Image.Resampling.BOX))
        coarse = Image.fromarray((edge_map(small) > EDGE).astype(np.uint8) * 255)
        coarse = np.asarray(coarse.resize((W, H), Image.Resampling.NEAREST)) > 0
        # Only borders the fine pass missed: near a sharp edge, coarse edges would
        # just thicken it and smear the object's outline.
        edges |= coarse & ~_filter(edges, ImageFilter.MaxFilter(2 * COARSE + 1))
    labels = label_components(solid & ~edges)
    n = labels.max()
    sizes = np.bincount(labels.ravel(), minlength=n + 1)
    big = np.flatnonzero(sizes >= MIN_REGION)
    big = big[big != 0]
    remap = np.full(n + 1, -1, np.int32)
    remap[big] = np.arange(len(big))
    lab = np.where(solid, remap[labels], -1).astype(np.int32)
    means = np.array([np.median(rgb[labels == r], 0) for r in big], np.float32)
    unresolved = solid & (lab < 0)
    lab[unresolved] = -2
    te._absorb_minor(lab, unresolved, rgb, means)  # edges / specks -> nearest neighbour
    if MERGE_EDGE > 0:
        lab = _merge_weak(lab, mag)
    lab = _recover_details(lab, rgb, solid)

    opts = te.TraceOptions(speckles=2, smooth_corners=1.0, optimize=0.2)
    order = sorted(range(lab.max() + 1), key=lambda k: -int((lab == k).sum()))
    below = ~solid
    defs, body = [], []
    if glow.any():
        # One blurred shape in the glow's own color, outlined where the glow is
        # half transparent, plus the solid object it surrounds (drawn on top).
        near = _filter(glow, ImageFilter.MaxFilter(GLOW_REACH))
        shape = (glow & (alpha >= 128)) | (solid & near)
        glow_paths = te._trace_mask(shape, opts, 1)
        if glow_paths:
            layer = te.Layer((0, 0, 0), glow_paths)
            defs.append('<filter id="glow" x="-20%" y="-20%" width="140%" height="140%">'
                        f'<feGaussianBlur stdDeviation="{GLOW_BLUR}"/></filter>')
            body.append(f'<path fill="{te._hex(tuple(int(v) for v in glow_color))}" '
                        f'filter="url(#glow)" d="{layer.svg_d()}"/>')
    for k in order:
        own = lab == k
        if not own.any():
            continue
        contours = te._trace_mask(te._dilate1(own) & solid, opts, 1,
                                  keep_hole=te._hole_checker(below))
        below |= own
        if not contours:
            continue
        fill = fit_fill(rgb, own, k)
        d = te.Layer((0, 0, 0), contours).svg_d()
        body.append(_path_with_fill(fill, d, defs))
        if own.sum() >= SOFT_MIN_REGION and SOFT_MODE != "off":
            clip_id = f"c{k}"
            soft = soft_shapes(rgb, own, fill, k, clip_id, opts, defs,
                               shadows_only=SOFT_MODE == "shadows")
            if soft:
                defs.append(f'<clipPath id="{clip_id}"><path d="{d}"/></clipPath>')
                body.extend(soft)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
            f'viewBox="0 0 {W} {H}">\n<defs>\n' + "\n".join(defs) + "\n</defs>\n"
            + "\n".join(body) + "\n</svg>\n")


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    svg = vectorize(src)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(f"wrote {dst}: {len(svg) // 1024} KB, {svg.count('<path')} paths, "
          f"{svg.count('<linearGradient')} gradients")
