# SPDX-License-Identifier: GPL-3.0-or-later
"""Centerline tracing: line art -> single stroked paths (like Autotrace's
centerline mode / Inkscape's "Centerline tracing").

Pipeline: binary mask -> Zhang-Suen thinning (skeleton) -> skeleton graph
(endpoints / junction clusters / edges) -> spur pruning -> chain merging at
2-way junctions -> smoothing + Ramer-Douglas-Peucker -> Catmull-Rom cubic
Beziers. Stroke width is estimated from ink area / skeleton length.
"""
from __future__ import annotations

import numpy as np

from trace_engine import Contour

_N8 = [(-1, 0), (0, 1), (1, 0), (0, -1), (-1, 1), (1, 1), (1, -1), (-1, -1)]  # 4-nbrs first


def thin(mask: np.ndarray) -> np.ndarray:
    """Zhang-Suen thinning (vectorized)."""
    ys = np.flatnonzero(mask.any(1))
    if ys.size == 0:
        return np.zeros_like(mask, bool)
    xs = np.flatnonzero(mask.any(0))
    y0, y1, x0, x1 = ys[0], ys[-1] + 1, xs[0], xs[-1] + 1
    img = np.pad(mask[y0:y1, x0:x1].astype(np.uint8), 1)
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            C = img[1:-1, 1:-1]
            P2, P3, P4 = img[:-2, 1:-1], img[:-2, 2:], img[1:-1, 2:]
            P5, P6, P7 = img[2:, 2:], img[2:, 1:-1], img[2:, :-2]
            P8, P9 = img[1:-1, :-2], img[:-2, :-2]
            B = P2 + P3 + P4 + P5 + P6 + P7 + P8 + P9
            seq = (P2, P3, P4, P5, P6, P7, P8, P9, P2)
            A = sum(((seq[i] == 0) & (seq[i + 1] == 1)).astype(np.uint8) for i in range(8))
            if step == 0:
                c1, c2 = P2 * P4 * P6, P4 * P6 * P8
            else:
                c1, c2 = P2 * P4 * P8, P2 * P6 * P8
            m = (C == 1) & (B >= 2) & (B <= 6) & (A == 1) & (c1 == 0) & (c2 == 0)
            if m.any():
                C[m] = 0
                changed = True
    # Remove redundant staircase pixels (keeps 8-connectivity, avoids fake junctions).
    C = img[1:-1, 1:-1]
    N, E, S, W = img[:-2, 1:-1], img[1:-1, 2:], img[2:, 1:-1], img[1:-1, :-2]
    NE, SE, SW, NW = img[:-2, 2:], img[2:, 2:], img[2:, :-2], img[:-2, :-2]
    for a, b, opp1, opp2, opp3 in ((N, E, S, W, SW), (E, S, N, W, NW), (S, W, N, E, NE),
                                   (W, N, S, E, SE)):
        m = (C == 1) & (a == 1) & (b == 1) & (opp1 == 0) & (opp2 == 0) & (opp3 == 0)
        C[m] = 0
    out = np.zeros_like(mask, bool)
    out[y0:y1, x0:x1] = img[1:-1, 1:-1].astype(bool)
    return out


def _rdp(pts: np.ndarray, eps: float) -> np.ndarray:
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        seg = pts[b] - pts[a]
        L = np.hypot(*seg)
        rel = pts[a + 1:b] - pts[a]
        if L < 1e-9:
            d = np.hypot(rel[:, 0], rel[:, 1])
        else:
            d = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / L
        i = int(d.argmax())
        if d[i] > eps:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    return pts[keep]


def _smooth(pts: np.ndarray, closed: bool, win: int = 2) -> np.ndarray:
    if len(pts) < 2 * win + 1:
        return pts
    k = 2 * win + 1
    if closed:
        p = np.concatenate([pts[-win:], pts, pts[:win]])
        out = np.stack([np.convolve(p[:, c], np.ones(k) / k, mode="valid") for c in (0, 1)], 1)
    else:
        out = pts.copy()
        inner = np.stack([np.convolve(pts[:, c], np.ones(k) / k, mode="valid") for c in (0, 1)], 1)
        out[win:-win] = inner
    return out


def _to_beziers(pts: np.ndarray, closed: bool, corner_deg: float = 55.0):
    """Catmull-Rom through points -> list of ('C', (x1,y1,x2,y2,x,y)).
    Sharp turns (> corner_deg) get zero tangents, so corners stay crisp."""
    n = len(pts)
    if n < 2:
        return []
    if n == 2:
        return [("L", tuple(pts[1]))]
    tang = np.zeros_like(pts)
    corner = np.zeros(n, bool)
    for i in range(n):
        if not closed and (i == 0 or i == n - 1):
            nb = pts[1] - pts[0] if i == 0 else pts[-1] - pts[-2]
            tang[i] = nb
            continue
        a, b = pts[(i - 1) % n], pts[(i + 1) % n]
        v1, v2 = pts[i] - a, b - pts[i]
        n1, n2 = np.hypot(*v1), np.hypot(*v2)
        if n1 > 1e-9 and n2 > 1e-9:
            cosang = float(np.dot(v1, v2) / (n1 * n2))
            if cosang < np.cos(np.radians(corner_deg)):
                corner[i] = True
        tang[i] = (b - a) / 2.0
    segs = []
    rng = range(n) if closed else range(n - 1)
    for i in rng:
        j = (i + 1) % n
        p0, p1 = pts[i], pts[j]
        d = np.hypot(*(p1 - p0))
        t0 = np.zeros(2) if corner[i] else tang[i]
        t1 = np.zeros(2) if corner[j] else tang[j]
        # limit handle length to a third of the segment
        def lim(t):
            L = np.hypot(*t)
            return t * min(1.0, d / max(L, 1e-9)) if L > 0 else t
        c1 = p0 + lim(t0) / 3.0
        c2 = p1 - lim(t1) / 3.0
        segs.append(("C", (c1[0], c1[1], c2[0], c2[1], p1[0], p1[1])))
    return segs


def trace_centerline(mask: np.ndarray, scale: int = 1, min_length: float = 3.0,
                     simplify: float = 0.6):
    """Return (contours, stroke_width) in original-image coordinates.
    Each contour is a trace_engine.Contour (open unless it is a loop)."""
    sk = thin(mask)
    if not sk.any():
        return [], 1.0
    H, W = sk.shape
    pad = np.pad(sk, 1)
    cnt = sum(pad[1 + dy:1 + dy + H, 1 + dx:1 + dx + W].astype(np.int8) for dy, dx in _N8)
    cnt = np.where(sk, cnt, 0)
    width = float(mask.sum()) / max(int(sk.sum()), 1) / scale

    node = sk & (cnt != 2)
    # cluster adjacent node pixels into one junction
    cluster = -np.ones(sk.shape, np.int32)
    centroids = []
    for y, x in zip(*np.nonzero(node)):
        if cluster[y, x] >= 0:
            continue
        cid = len(centroids)
        stack, members = [(y, x)], []
        cluster[y, x] = cid
        while stack:
            cy, cx = stack.pop()
            members.append((cy, cx))
            for dy, dx in _N8:
                ny, nx = cy + dy, cx + dx
                if 0 <= ny < H and 0 <= nx < W and node[ny, nx] and cluster[ny, nx] < 0:
                    cluster[ny, nx] = cid
                    stack.append((ny, nx))
        m = np.array(members, float)
        centroids.append((m[:, 1].mean(), m[:, 0].mean()))  # (x, y)

    visited = np.zeros(sk.shape, bool)
    edges = []  # (cluster_a, cluster_b, [(x,y),...])

    def walk(sy, sx, py, px):
        pts = []
        cy, cx = sy, sx
        while True:
            visited[cy, cx] = True
            pts.append((cx, cy))
            nxt = None
            for dy, dx in _N8:
                ny, nx = cy + dy, cx + dx
                if not (0 <= ny < H and 0 <= nx < W) or not sk[ny, nx] or (ny, nx) == (py, px):
                    continue
                if node[ny, nx]:
                    if len(pts) > 1 or (ny, nx) != (py, px):
                        return pts, int(cluster[ny, nx])
                elif not visited[ny, nx]:
                    nxt = (ny, nx)
                    break
            if nxt is None:
                return pts, -1
            py, px = cy, cx
            cy, cx = nxt

    for y, x in zip(*np.nonzero(node)):
        a = int(cluster[y, x])
        for dy, dx in _N8:
            ny, nx = y + dy, x + dx
            if not (0 <= ny < H and 0 <= nx < W) or not sk[ny, nx] or node[ny, nx] \
                    or visited[ny, nx]:
                continue
            pts, b = walk(ny, nx, y, x)
            if a == b and len(pts) <= 2:
                continue  # wiggle inside a junction cluster
            edges.append([a, b, pts])
    # closed loops without nodes
    loops = []
    for y, x in zip(*np.nonzero(sk & ~visited & ~node)):
        if visited[y, x]:
            continue
        pts, _ = walk(y, x, -9, -9)
        loops.append(pts)

    # degree of each cluster in the edge graph
    def degrees():
        deg = {}
        for e in edges:
            for c in (e[0], e[1]):
                if c >= 0:
                    deg[c] = deg.get(c, 0) + 1
        return deg

    # prune short spurs (endpoint -> junction)
    spur = max(2.0 * width * scale, 3.0 * scale)
    deg = degrees()
    edges = [e for e in edges if not (
        len(e[2]) < spur and ((deg.get(e[0], 0) == 1 and deg.get(e[1], 0) >= 3) or
                              (deg.get(e[1], 0) == 1 and deg.get(e[0], 0) >= 3)))]

    # attach junction centroids so strokes meet
    def full(e):
        pts = list(e[2])
        if e[0] >= 0:
            pts.insert(0, centroids[e[0]])
        if e[1] >= 0:
            pts.append(centroids[e[1]])
        return pts

    paths = [[e[0], e[1], full(e)] for e in edges]
    # merge chains through clusters that join exactly two paths
    changed = True
    while changed:
        changed = False
        ends = {}
        for i, p in enumerate(paths):
            for side, c in ((0, p[0]), (1, p[1])):
                if c >= 0:
                    ends.setdefault(c, []).append((i, side))
        for c, lst in ends.items():
            if len(lst) != 2 or lst[0][0] == lst[1][0]:
                continue
            (i, si), (j, sj) = lst
            pi, pj = paths[i], paths[j]
            a = pi[2] if si == 1 else pi[2][::-1]  # ends at c
            b = pj[2] if sj == 0 else pj[2][::-1]  # starts at c
            start = pi[0] if si == 1 else pi[1]
            end = pj[1] if sj == 0 else pj[0]
            paths[i] = [start, end, a + b[1:]]
            paths.pop(j)
            changed = True
            break

    contours = []
    k = 1.0 / scale
    for p in paths:
        pts = np.array(p[2], float)
        closed = p[0] >= 0 and p[0] == p[1] and len(pts) > 4
        if len(pts) < 2 or (len(pts) * k < min_length and p[0] < 0 and p[1] < 0):
            continue
        contours.append(_finish(pts, closed, scale, simplify))
    for pts in loops:
        pts = np.array(pts, float)
        if len(pts) * k < min_length:
            continue
        contours.append(_finish(pts, True, scale, simplify))
    return [c for c in contours if c], width


def _finish(pts, closed, scale, simplify):
    pts = _smooth(pts, closed, win=max(1, scale))
    if closed and np.hypot(*(pts[0] - pts[-1])) < 1.5 * scale:
        pts = pts[:-1]
    pts = _rdp(pts, simplify * scale)
    if len(pts) < 2:
        return None
    k = 1.0 / scale
    pts = pts * k
    segs = _to_beziers(pts, closed)
    return Contour((pts[0][0], pts[0][1]), segs, closed)
