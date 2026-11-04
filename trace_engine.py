"""Bitmap -> vector tracing engine (libpotrace via ctypes).

Modes mirror Inkscape's "Trace Bitmap" dialog:
  single scan : brightness cutoff, edge detection, color quantization
  multi scan  : brightness steps, colors, grays

Quality pipeline (applied before potrace):
  1. edge-preserving denoise (bilateral)       - removes JPEG noise
  2. upscale (Lanczos)                          - smoother curves, crisp small text
  3. perceptual palette (CIELAB)                - merges look-alike shades, keeps
                                                  small vivid colors, drops
                                                  anti-aliasing blend colors
  4. neighbour-vote for blend pixels + majority smoothing of color regions
  5. optional OCR (tesseract): text is removed from the bitmap and emitted as
     real, editable SVG <text>
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

import numpy as np
from PIL import Image, ImageFilter

# --------------------------------------------------------------------------
# libpotrace bindings
# --------------------------------------------------------------------------
_lib = ctypes.CDLL(ctypes.util.find_library("potrace") or "libpotrace.so.0")

POTRACE_CURVETO = 1
POTRACE_CORNER = 2
potrace_word = ctypes.c_ulong
WORD_BITS = ctypes.sizeof(potrace_word) * 8


class _Progress(ctypes.Structure):
    _fields_ = [("callback", ctypes.c_void_p), ("data", ctypes.c_void_p),
                ("min", ctypes.c_double), ("max", ctypes.c_double),
                ("epsilon", ctypes.c_double)]


class _Param(ctypes.Structure):
    _fields_ = [("turdsize", ctypes.c_int), ("turnpolicy", ctypes.c_int),
                ("alphamax", ctypes.c_double), ("opticurve", ctypes.c_int),
                ("opttolerance", ctypes.c_double), ("progress", _Progress)]


class _Bitmap(ctypes.Structure):
    _fields_ = [("w", ctypes.c_int), ("h", ctypes.c_int), ("dy", ctypes.c_int),
                ("map", ctypes.POINTER(potrace_word))]


class _DPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _Curve(ctypes.Structure):
    _fields_ = [("n", ctypes.c_int), ("tag", ctypes.POINTER(ctypes.c_int)),
                ("c", ctypes.POINTER(_DPoint * 3))]


class _Path(ctypes.Structure):
    pass


_Path._fields_ = [("area", ctypes.c_int), ("sign", ctypes.c_int), ("curve", _Curve),
                  ("next", ctypes.POINTER(_Path)), ("childlist", ctypes.POINTER(_Path)),
                  ("sibling", ctypes.POINTER(_Path)), ("priv", ctypes.c_void_p)]


class _State(ctypes.Structure):
    _fields_ = [("status", ctypes.c_int), ("plist", ctypes.POINTER(_Path)),
                ("priv", ctypes.c_void_p)]


_lib.potrace_param_default.restype = ctypes.POINTER(_Param)
_lib.potrace_param_free.argtypes = [ctypes.POINTER(_Param)]
_lib.potrace_trace.restype = ctypes.POINTER(_State)
_lib.potrace_trace.argtypes = [ctypes.POINTER(_Param), ctypes.POINTER(_Bitmap)]
_lib.potrace_state_free.argtypes = [ctypes.POINTER(_State)]

# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
DEFAULT_FONT = "Segoe UI, Noto Sans, Arial, sans-serif"


@dataclass
class Layer:
    color: tuple[int, int, int]
    contours: list = field(default_factory=list)  # (start, segs[, closed])
    stroke_width: float = 0.0  # > 0: centerline strokes instead of fills

    def svg_d(self, precision: int = 1) -> str:
        """Compact path data: relative commands, few decimals, no redundant
        separators (coordinates are in original-image pixels, so 0.1px is
        plenty)."""
        q = 10 ** precision

        def num(v):
            t = f"{round(v * q) / q:.{precision}f}".rstrip("0").rstrip(".")
            return "0" if t in ("-0", "", "-") else t

        def join(vals):
            out = ""
            for v in vals:
                t = num(v)
                out += t if (not out or t.startswith("-")) else " " + t
            return out

        parts = []
        for c in self.contours:
            (sx, sy), segs = c[0], c[1]
            closed = c[2] if len(c) > 2 else True
            cx, cy = round(sx * q) / q, round(sy * q) / q
            parts.append("M" + join((cx, cy)))
            for kind, pts in segs:
                rel = []
                for i in range(0, len(pts), 2):
                    rel += [pts[i] - cx, pts[i + 1] - cy]
                ex, ey = pts[-2], pts[-1]
                # accumulate from rounded values so errors don't drift
                rel = [round(v * q) / q for v in rel]
                cx, cy = cx + rel[-2], cy + rel[-1]
                parts.append(kind.lower() + join(rel))
            if closed:
                parts.append("z")
        return "".join(parts)


@dataclass
class TextItem:
    x: float  # left
    y: float  # baseline
    size: float  # font size (px)
    width: float  # target advance width (px)
    text: str
    color: tuple[int, int, int]


_FONT_CACHE: dict = {}


def _measure(family: str, size: float, text: str) -> float | None:
    """Advance width of text in the font this machine resolves ``family`` to."""
    first = family.split(",")[0].strip().strip("'\"") or "sans-serif"
    if first not in _FONT_CACHE:
        path = None
        try:
            path = subprocess.run(["fc-match", "-f", "%{file}", first], capture_output=True,
                                  text=True, timeout=10).stdout.strip() or None
        except Exception:  # noqa: BLE001
            pass
        _FONT_CACHE[first] = path
    path = _FONT_CACHE[first]
    if not path:
        return None
    try:
        from PIL import ImageFont
        return ImageFont.truetype(path, 100).getlength(text) * size / 100.0
    except Exception:  # noqa: BLE001
        return None


@dataclass
class TraceResult:
    width: int
    height: int
    layers: list[Layer]
    scale: int = 1
    texts: list[TextItem] = field(default_factory=list)
    font_family: str = DEFAULT_FONT

    @property
    def path_count(self) -> int:
        return sum(len(l.contours) for l in self.layers)

    @property
    def node_count(self) -> int:
        return sum(len(c[1]) for l in self.layers for c in l.contours)

    def to_svg(self) -> str:
        out = [
            '<?xml version="1.0" encoding="UTF-8"?>\n',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" '
            f'height="{self.height}" viewBox="0 0 {self.width} {self.height}">\n',
        ]
        for layer in self.layers:
            if not layer.contours:
                continue
            r, g, b = layer.color
            if layer.stroke_width > 0:
                out.append(f'  <path fill="none" stroke="#{r:02x}{g:02x}{b:02x}" '
                           f'stroke-width="{layer.stroke_width:.2f}" stroke-linecap="round" '
                           f'stroke-linejoin="round" d="{layer.svg_d()}"/>\n')
                continue
            out.append(f'  <path fill="#{r:02x}{g:02x}{b:02x}" '
                       f'd="{layer.svg_d()}"/>\n')
        if self.texts:
            out.append(f'  <g font-family="{escape(self.font_family)}">\n')
            for t in self.texts:
                r, g, b = t.color
                # letter-spacing makes the width right even where textLength
                # is not supported; textLength then fine-tunes in browsers.
                nat = _measure(self.font_family, t.size, t.text)
                ls = ""
                if nat and len(t.text) > 1:
                    ls = f' letter-spacing="{(t.width - nat) / len(t.text):.2f}"'
                out.append(
                    f'    <text x="{t.x:.2f}" y="{t.y:.2f}" font-size="{t.size:.2f}"{ls} '
                    f'textLength="{t.width:.2f}" lengthAdjust="spacingAndGlyphs" '
                    f'fill="#{r:02x}{g:02x}{b:02x}" xml:space="preserve">{escape(t.text)}</text>\n')
            out.append("  </g>\n")
        out.append("</svg>\n")
        return "".join(out)


@dataclass
class TraceOptions:
    mode: str = "brightness"  # brightness|edge|quant|multi_brightness|multi_colors|multi_grays
    threshold: float = 0.45  # brightness cutoff / edge threshold (0..1)
    quant_colors: int = 8  # color quantization (single scan)
    scans: int = 8  # multi-scan count (max colors)
    invert: bool = False
    smooth: bool = False  # blur before multi scans
    stack: bool = True  # stack multi scans
    remove_background: bool = False
    # potrace options
    speckles: int = 2  # turdsize, in original-image pixels
    smooth_corners: float = 1.0  # alphamax
    optimize: float = 0.2  # opttolerance
    optimize_enabled: bool = True
    # quality options
    upscale: int = 0  # 0 = auto, otherwise 1..4
    merge_below: float = 1.0  # blend (anti-aliasing) colors under this % are dropped
    merge_de: float = 8.0  # colors closer than this (CIELAB delta-E) are merged
    keep_transparency: bool = True
    edge_smooth: float = 1.0  # smooth color-region boundaries (original px)
    denoise: float = 0.0  # edge-preserving denoise strength (0 = off)
    cleanup: bool = True  # remove anti-aliasing/halo colors (logos); off = faithful
    noise_speckles: int = 6  # speckle size for low-contrast colors (halos, shade noise)
    palette: str = ""  # fixed palette "#112233, #445566" (empty = automatic)
    upscaler: str = "lanczos"  # lanczos | an AI backend name from ai_upscalers()
    stroke_width: float = 0.0  # centerline: 0 = auto
    # OCR
    ocr: bool = False
    ocr_min_conf: float = 70.0
    font_family: str = DEFAULT_FONT


def parse_palette(text: str) -> list[tuple[int, int, int]]:
    """'#1b1b1b, e0c088 #fff' -> [(27,27,27), (224,192,136), (255,255,255)]"""
    import re
    out = []
    for tok in re.findall(r"(?<![0-9a-zA-Z])#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])",
                          text or ""):
        if len(tok) == 3:
            tok = "".join(ch * 2 for ch in tok)
        out.append(tuple(int(tok[i:i + 2], 16) for i in (0, 2, 4)))
    return out


# AI upscaler backends (ncnn/Vulkan CLIs). name -> (binary, native scales, args builder)
_AI_BACKENDS = {
    "Real-ESRGAN": ("realesrgan-ncnn-vulkan", (4,),
                    lambda i, o, s: ["-i", i, "-o", o, "-s", "4", "-n", "realesrgan-x4plus"]),
    "Real-CUGAN": ("realcugan-ncnn-vulkan", (2, 3, 4),
                   lambda i, o, s: ["-i", i, "-o", o, "-s", str(s), "-n", "0"]),
    "waifu2x": ("waifu2x-ncnn-vulkan", (2, 4),
                lambda i, o, s: ["-i", i, "-o", o, "-s", str(s), "-n", "1"]),
}
_AI_CACHE: dict = {}


def ai_upscalers() -> list[str]:
    return [n for n, (b, _, _) in _AI_BACKENDS.items() if shutil.which(b)]


def _ai_upscale(rgb: np.ndarray, scale: int, backend: str) -> np.ndarray | None:
    """Upscale with an AI model; returns None if unavailable/failed."""
    if backend not in _AI_BACKENDS or not shutil.which(_AI_BACKENDS[backend][0]):
        return None
    key = (backend, scale, rgb.shape, hash(rgb[::5, ::5].tobytes()))
    if _AI_CACHE.get("key") == key:
        return _AI_CACHE["out"]
    binary, natives, args = _AI_BACKENDS[backend]
    native = min((n for n in natives if n >= scale), default=max(natives))
    tmp = tempfile.mkdtemp(prefix="imgtrace-ai-")
    try:
        src, dst = os.path.join(tmp, "in.png"), os.path.join(tmp, "out.png")
        Image.fromarray(rgb).save(src)
        cmd = [binary] + args(src, dst, native)
        # Point at packaged model folders when the binary doesn't find them itself.
        for mdir in {"waifu2x": ["/usr/share/waifu2x-ncnn-vulkan/models-cunet"],
                     "Real-ESRGAN": ["/usr/share/realesrgan-ncnn-vulkan/models",
                                     "/usr/share/realesrgan-ncnn-vulkan"],
                     "Real-CUGAN": ["/usr/share/realcugan-ncnn-vulkan/models-se"]}.get(backend, []):
            if os.path.isdir(mdir):
                cmd += ["-m", mdir]
                break
        subprocess.run(cmd, capture_output=True, timeout=600)
        if not os.path.exists(dst):
            return None
        out = Image.open(dst).convert("RGB")
        h, w = rgb.shape[:2]
        if out.size != (w * scale, h * scale):
            out = out.resize((w * scale, h * scale), Image.Resampling.LANCZOS)
        res = np.asarray(out)
    except Exception:  # noqa: BLE001
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _AI_CACHE.update(key=key, out=res)
    return res


AUTO_TARGET = 2000  # auto-upscale small images until the long side is ~this


def effective_scale(w: int, h: int, opts: TraceOptions) -> int:
    if opts.upscale:
        return max(1, min(4, int(opts.upscale)))
    return max(1, min(4, AUTO_TARGET // max(w, h, 1)))


_OCR_STATUS = None


def ocr_status() -> tuple[bool, str]:
    """(usable, message). Needs the tesseract binary and English data."""
    global _OCR_STATUS
    if _OCR_STATUS is None or not _OCR_STATUS[0]:  # re-check until it works
        if not shutil.which("tesseract"):
            _OCR_STATUS = (False, "OCR needs tesseract: sudo pacman -S tesseract tesseract-data-eng")
        else:
            try:
                out = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                                     text=True, timeout=20).stdout.split()
            except Exception:  # noqa: BLE001
                out = []
            if "eng" in out:
                _OCR_STATUS = (True, "OCR ready")
            else:
                _OCR_STATUS = (False, "OCR needs English data: sudo pacman -S tesseract-data-eng")
    return _OCR_STATUS


def ocr_available() -> bool:
    return ocr_status()[0]


# --------------------------------------------------------------------------
# potrace wrapper
# --------------------------------------------------------------------------
def _trace_mask(mask: np.ndarray, opts: TraceOptions, scale: int = 1,
                keep_hole=None, speckles: int | None = None) -> list:
    """Trace a boolean mask (True = filled). Returns contours in original-image
    coordinates (divided by ``scale``).

    ``keep_hole(points)`` - optional; called with each hole outline (N x 2,
    mask coordinates). Returning False drops the hole, i.e. fills it (used when
    the hole will be covered by shapes drawn on top anyway)."""
    ys = np.flatnonzero(mask.any(1))
    if ys.size == 0:
        return []
    xs = np.flatnonzero(mask.any(0))
    y0, y1 = max(0, ys[0] - 1), min(mask.shape[0], ys[-1] + 2)
    x0, x1 = max(0, xs[0] - 1), min(mask.shape[1], xs[-1] + 2)
    sub = mask[y0:y1, x0:x1]
    h, w = sub.shape
    packed = np.packbits(sub, axis=1, bitorder="big")
    word_bytes = WORD_BITS // 8
    pad = (-packed.shape[1]) % word_bytes
    if pad:
        packed = np.pad(packed, ((0, 0), (0, pad)))
    words = np.ascontiguousarray(packed.view(f">u{word_bytes}").astype(f"=u{word_bytes}"))
    bm = _Bitmap(w, h, words.shape[1], words.ctypes.data_as(ctypes.POINTER(potrace_word)))
    param = _lib.potrace_param_default()
    try:
        p = param.contents
        p.turdsize = int(opts.speckles if speckles is None else speckles) * scale * scale
        p.alphamax = float(opts.smooth_corners)
        p.opticurve = 1 if opts.optimize_enabled else 0
        p.opttolerance = float(opts.optimize) * scale
        state = _lib.potrace_trace(param, ctypes.byref(bm))
    finally:
        _lib.potrace_param_free(param)
    if not state or state.contents.status != 0:
        if state:
            _lib.potrace_state_free(state)
        raise RuntimeError("potrace failed")

    k = 1.0 / scale
    contours = []
    try:
        node = state.contents.plist
        while node:
            pth = node.contents
            curve = pth.curve
            n = curve.n
            tags, c = curve.tag, curve.c
            if keep_hole is not None and pth.sign == 45:  # '-' = hole
                pts = np.array([(c[i][2].x + x0, c[i][2].y + y0) for i in range(n)])
                if not keep_hole(pts):
                    node = pth.next
                    continue
            last = c[n - 1][2]
            segs = []
            for i in range(n):
                seg = c[i]
                if tags[i] == POTRACE_CORNER:
                    segs.append(("L", ((seg[1].x + x0) * k, (seg[1].y + y0) * k)))
                    segs.append(("L", ((seg[2].x + x0) * k, (seg[2].y + y0) * k)))
                else:
                    segs.append(("C", ((seg[0].x + x0) * k, (seg[0].y + y0) * k,
                                       (seg[1].x + x0) * k, (seg[1].y + y0) * k,
                                       (seg[2].x + x0) * k, (seg[2].y + y0) * k)))
            contours.append((((last.x + x0) * k, (last.y + y0) * k), segs))
            node = pth.next
    finally:
        _lib.potrace_state_free(state)
    return contours


def _dilate1(m: np.ndarray) -> np.ndarray:
    out = m.copy()
    out[1:] |= m[:-1]
    out[:-1] |= m[1:]
    out[:, 1:] |= m[:, :-1]
    out[:, :-1] |= m[:, 1:]
    return out


def _hole_checker(below: np.ndarray):
    """keep_hole callback: keep a hole only if it would reveal pixels that are
    painted *below* this layer (earlier layers / transparency)."""
    from PIL import ImageDraw
    H, W = below.shape

    def keep(pts):
        bx0, by0 = np.floor(pts.min(0)).astype(int)
        bx1, by1 = np.ceil(pts.max(0)).astype(int)
        bx0, by0 = max(bx0, 0), max(by0, 0)
        bx1, by1 = min(bx1, W), min(by1, H)
        if bx1 - bx0 < 1 or by1 - by0 < 1:
            return False
        region = below[by0:by1, bx0:bx1]
        if not region.any():
            return False
        im = Image.new("1", (bx1 - bx0, by1 - by0), 0)
        ImageDraw.Draw(im).polygon([(x - bx0, y - by0) for x, y in pts], fill=1)
        inside = np.asarray(im)
        hits = int((region & inside).sum())
        return hits > max(2, 0.02 * int(inside.sum()))

    return keep


# --------------------------------------------------------------------------
# Color helpers
# --------------------------------------------------------------------------
def load_image(path: str) -> Image.Image:
    img = Image.open(path)
    img.load()
    return img.convert("RGBA")


def _pack_rgb(px: np.ndarray) -> np.ndarray:
    px = px.astype(np.int32)
    return (px[..., 0] << 16) | (px[..., 1] << 8) | px[..., 2]


def _mode_color(px: np.ndarray) -> tuple[int, int, int]:
    """Most frequent exact RGB value among pixels (N x 3)."""
    vals, counts = np.unique(_pack_rgb(px), return_counts=True)
    v = int(vals[counts.argmax()])
    return (v >> 16) & 255, (v >> 8) & 255, v & 255


_M_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                   [0.2126729, 0.7151522, 0.0721750],
                   [0.0193339, 0.1191920, 0.9503041]], np.float32)
_WHITE = np.array([0.95047, 1.0, 1.08883], np.float32)


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    c = np.asarray(rgb, np.float32) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = (c @ _M_XYZ.T) / _WHITE
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], -1)


# 6-bit-per-channel lookup grid (64^3 cells) used for fast pixel classification.
_GRID_LAB = None


def _grid_lab():
    global _GRID_LAB
    if _GRID_LAB is None:
        v = np.arange(64, dtype=np.float32) * 4 + 2
        r, g, b = np.meshgrid(v, v, v, indexing="ij")
        _GRID_LAB = _srgb_to_lab(np.stack([r, g, b], -1).reshape(-1, 3))
    return _GRID_LAB


def _grid_key(rgb: np.ndarray) -> np.ndarray:
    q = rgb.astype(np.int32) >> 2
    return (q[..., 0] << 12) | (q[..., 1] << 6) | q[..., 2]


# --------------------------------------------------------------------------
# Preprocessing
# --------------------------------------------------------------------------
def _bilateral(rgb: np.ndarray, strength: float) -> np.ndarray:
    """Edge-preserving smoothing: flattens JPEG noise, keeps text edges sharp."""
    if strength <= 0:
        return rgb
    img = rgb.astype(np.float32)
    H, W = img.shape[:2]
    r = 2
    inv_ss = 1.0 / (2 * 1.2 ** 2)
    inv_sr = 1.0 / (2 * (10.0 * strength) ** 2 * 3)
    pad = np.pad(img, ((r, r), (r, r), (0, 0)), mode="edge")
    acc = np.zeros_like(img)
    wsum = np.zeros((H, W), np.float32)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            nb = pad[r + dy:r + dy + H, r + dx:r + dx + W]
            d2 = ((nb - img) ** 2).sum(-1)
            wgt = np.exp(-(dy * dy + dx * dx) * inv_ss - d2 * inv_sr)
            acc += nb * wgt[..., None]
            wsum += wgt
    return np.clip(acc / wsum[..., None] + 0.5, 0, 255).astype(np.uint8)


def _edge_mask(rgb: np.ndarray, threshold: float, scale: int) -> np.ndarray:
    img = Image.fromarray(rgb).convert("L").filter(ImageFilter.GaussianBlur(1.0 * scale))
    gray = np.asarray(img, dtype=np.float32)
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[1:-1, 1:-1] = (gray[:-2, 2:] + 2 * gray[1:-1, 2:] + gray[2:, 2:]
                      - gray[:-2, :-2] - 2 * gray[1:-1, :-2] - gray[2:, :-2])
    gy[1:-1, 1:-1] = (gray[2:, :-2] + 2 * gray[2:, 1:-1] + gray[2:, 2:]
                      - gray[:-2, :-2] - 2 * gray[:-2, 1:-1] - gray[:-2, 2:])
    mag = np.hypot(gx, gy)
    mx = mag.max()
    if mx <= 0:
        return np.zeros(gray.shape, bool)
    return (mag / mx) > (threshold * 0.5)


def _luminance(rgb: np.ndarray) -> np.ndarray:
    rgb = rgb.astype(np.float32)
    return (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]) / 255.0


# --------------------------------------------------------------------------
# OCR (tesseract)
# --------------------------------------------------------------------------
_OCR_CACHE: dict = {}


def _run_tesseract(gray: Image.Image, factor: int) -> list:
    """Return words [(x0,y0,x1,y1,text,conf)] in original coordinates."""
    tmp = tempfile.mkdtemp(prefix="imgtrace-")
    try:
        path = os.path.join(tmp, "in.png")
        gray.save(path)
        proc = subprocess.run(["tesseract", path, "stdout", "--psm", "11", "tsv"],
                              capture_output=True, text=True, timeout=120)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    words = []
    for line in proc.stdout.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        text = cols[11].strip()
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if not text or conf < 0:
            continue
        x, y, w, h = (int(v) / factor for v in cols[6:10])
        words.append((x, y, x + w, y + h, text, conf))
    return words


def _ocr_words(rgb1x: np.ndarray, min_conf: float) -> list:
    key = (rgb1x.shape, hash(rgb1x[::7, ::7].tobytes()), min_conf)
    if _OCR_CACHE.get("key") == key:
        return _OCR_CACHE["words"]
    h, w = rgb1x.shape[:2]
    factor = max(1, min(4, 2400 // max(w, h, 1)))
    gray = Image.fromarray(rgb1x).convert("L")
    if factor > 1:
        gray = gray.resize((w * factor, h * factor), Image.Resampling.LANCZOS)
    inv = Image.fromarray(255 - np.asarray(gray))
    # Run on both polarities (light-on-dark and dark-on-light text).
    cands = _run_tesseract(gray, factor) + _run_tesseract(inv, factor)
    # Accept confident words, plus low-confidence ones (>=30) that are numbers
    # or repeat a word read confidently elsewhere (dim table cells, etc.).
    import re
    vocab = {c[4] for c in cands if c[5] >= min_conf}
    numeric = re.compile(r"^[0-9][0-9.,:%/-]*$")
    cands = [c for c in cands
             if any(ch.isalnum() for ch in c[4]) and (c[3] - c[1]) >= 5
             and (c[5] >= min_conf or (c[5] >= 30 and (c[4] in vocab or numeric.match(c[4]))))]
    cands.sort(key=lambda c: -c[5])
    words = []
    for c in cands:
        area = (c[2] - c[0]) * (c[3] - c[1])
        clash = False
        for d in words:
            ix = min(c[2], d[2]) - max(c[0], d[0])
            iy = min(c[3], d[3]) - max(c[1], d[1])
            if ix > 0 and iy > 0 and ix * iy > 0.4 * min(area, (d[2] - d[0]) * (d[3] - d[1])):
                clash = True
                break
        if not clash:
            words.append(c)
    _OCR_CACHE.update(key=key, words=words)
    return words


def _group_lines(words: list) -> list:
    """Group words into text lines: [(x0,y0,x1,y1,text)]."""
    lines = []  # each: [x0,y0,x1,y1,[texts]]
    for x0, y0, x1, y1, text, _ in sorted(words, key=lambda c: c[0]):
        h = y1 - y0
        best = None
        for ln in lines:
            lh = ln[3] - ln[1]
            ov = min(y1, ln[3]) - max(y0, ln[1])
            gap = x0 - ln[2]
            if ov > 0.5 * min(h, lh) and 0.4 < h / lh < 2.5 and -2 <= gap <= 0.9 * max(h, lh):
                best = ln
                break
        if best:
            best[0], best[1] = min(best[0], x0), min(best[1], y0)
            best[2], best[3] = max(best[2], x1), max(best[3], y1)
            best[4].append(text)
        else:
            lines.append([x0, y0, x1, y1, [text]])
    return [(a, b, c, d, " ".join(t)) for a, b, c, d, t in lines]


def _clean_words(words: list) -> list:
    """Drop icons that OCR misreads as 1-2 character 'words' ('2', 'DI', 'O',
    'X'). A short token is kept when it follows a word ("0.1 MB", "go to") or
    precedes another short token ("0 MB"); it is dropped when it stands alone
    or sits right before a longer label (an icon in front of its text)."""
    def same_line(a, b):
        h = max(a[3] - a[1], b[3] - b[1])
        return abs((a[1] + a[3]) - (b[1] + b[3])) < h

    out = []
    for w in words:
        x0, y0, x1, y1, text, conf = w
        if len(text) > 2:
            out.append(w)
            continue
        h = y1 - y0

        def gap(v):
            return 0.9 * max(h, v[3] - v[1], 6)

        left = [v for v in words if v is not w and same_line(v, w) and 0 <= x0 - v[2] <= gap(v)]
        right = [v for v in words if v is not w and same_line(v, w) and 0 <= v[0] - x1 <= gap(v)]
        if left or (right and min(len(v[4]) for v in right) <= 2):
            out.append(w)
    return out


def _fix_unit_spaces(text: str, ink) -> str:
    """'0MB' -> '0 MB' when the pixels show a word-sized gap between the
    number and the unit (OCR often merges them)."""
    import re
    if ink is None or not re.search(r"\d[A-Za-z]", text):
        return text
    cols = ink.any(0)
    h = max(int(np.ptp(np.flatnonzero(ink.any(1)))) + 1, 1) if ink.any() else 1
    # empty-column runs inside the ink extent
    nz = np.flatnonzero(cols)
    if nz.size == 0:
        return text
    inner = cols[nz[0]:nz[-1] + 1]
    runs, cur = [], 0
    for c in inner:
        if not c:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    wide = sum(1 for r in runs if r >= 0.22 * h)
    if wide > text.count(" "):
        text = re.sub(r"(\d)([A-Za-z]{1,3})\b", r"\1 \2", text)
    return text


_ASC = set("bdfhklt()[]{}/\\|!?%#@&$'\"")
_DESC = set("gjpqy,;()[]{}|_")


def _apply_ocr(rgb1x: np.ndarray, opts: TraceOptions):
    """Detect text, return (rgb with text painted out, [TextItem])."""
    lines = _group_lines(_clean_words(_ocr_words(rgb1x, opts.ocr_min_conf)))
    out = rgb1x.copy()
    H, W = out.shape[:2]
    items = []
    for x0, y0, x1, y1, text in lines:
        ix0, iy0 = max(0, int(np.floor(x0)) - 1), max(0, int(np.floor(y0)) - 1)
        ix1, iy1 = min(W, int(np.ceil(x1)) + 1), min(H, int(np.ceil(y1)) + 1)
        if ix1 - ix0 < 2 or iy1 - iy0 < 2:
            continue
        # background = median of a ring around the box
        rx0, ry0, rx1, ry1 = max(0, ix0 - 1), max(0, iy0 - 1), min(W, ix1 + 1), min(H, iy1 + 1)
        ring = np.concatenate([rgb1x[ry0, rx0:rx1], rgb1x[ry1 - 1, rx0:rx1],
                               rgb1x[ry0:ry1, rx0], rgb1x[ry0:ry1, rx1 - 1]])
        bg = np.median(ring, axis=0)
        box = rgb1x[iy0:iy1, ix0:ix1].reshape(-1, 3).astype(np.float32)
        d = np.sqrt(((box - bg) ** 2).sum(-1))
        if d.max() < 40:
            continue
        # Text color by unmixing: each pixel = a*fg + (1-a)*bg. Coverage a comes
        # from luminance (kept sharp by JPEG); summing (p - bg) over the box and
        # dividing by the summed coverage recovers the true color even though
        # JPEG smears chroma into halos.
        lum = _luminance(box) * 255
        dl = lum - float(_luminance(bg[None])[0] * 255)
        sgn = 1.0 if dl.max() >= -dl.min() else -1.0
        D = float(np.percentile(dl * sgn, 97))
        if D < 15:
            continue
        a = np.clip(dl * sgn / D, 0, 1)
        fg = np.clip(bg + (box - bg).sum(0) / max(a.sum(), 1e-6), 0, 255)
        # Use the actual ink extent (tesseract boxes can include borders).
        ink = (a.reshape(iy1 - iy0, ix1 - ix0) > 0.35)
        rows, cols = np.flatnonzero(ink.any(1)), np.flatnonzero(ink.any(0))
        if rows.size and cols.size:
            y0, y1 = iy0 + rows[0], iy0 + rows[-1] + 1
            x0, x1 = ix0 + cols[0], ix0 + cols[-1] + 1
        out[iy0:iy1, ix0:ix1] = bg.astype(np.uint8)

        text = _fix_unit_spaces(text, ink if rows.size and cols.size else None)
        top = 0.73 if any(ch in _ASC or ch.isupper() or ch.isdigit() for ch in text) else 0.53
        bottom = 0.22 if any(ch in _DESC for ch in text) else 0.0
        em = (y1 - y0) / (top + bottom)
        items.append(TextItem(x0, y1 - bottom * em, em, x1 - x0, text,
                              tuple(int(v) for v in fg)))
    return out, items


# --------------------------------------------------------------------------
# Palette + classification
# --------------------------------------------------------------------------
def _cluster_colors(px: np.ndarray, merge_de: float):
    """Leader clustering in CIELAB. Shades closer than ``merge_de`` merge, so
    JPEG noise / near-identical tones collapse into one color, while small but
    clearly different colors keep their own. Returns [(mean_rgb, count)]."""
    total = len(px)
    q = px.astype(np.int32) >> 3
    key = (q[:, 0] << 10) | (q[:, 1] << 5) | q[:, 2]
    bins, inv, counts = np.unique(key, return_inverse=True, return_counts=True)
    means = np.stack([np.bincount(inv, px[:, c].astype(np.float64), len(bins))
                      for c in range(3)], 1) / counts[:, None]
    labs = _srgb_to_lab(means)
    order = np.argsort(-counts)
    cap = len(order)
    c_lab = np.zeros((cap, 3), np.float32)
    c_sum = np.zeros((cap, 3), np.float64)
    c_n = np.zeros(cap, np.int64)
    k = 0
    de2 = merge_de * merge_de
    for i in order:
        if k:
            d = ((c_lab[:k] - labs[i]) ** 2).sum(1)
            j = int(d.argmin())
            if d[j] < de2:
                c_sum[j] += means[i] * counts[i]
                c_n[j] += counts[i]
                continue
        c_lab[k] = labs[i]
        c_sum[k] = means[i] * counts[i]
        c_n[k] = counts[i]
        k += 1
    min_px = max(8, total * 0.0002)
    clusters = [(c_sum[j] / c_n[j], int(c_n[j])) for j in range(k) if c_n[j] >= min_px]
    if not clusters:
        j = int(c_n[:k].argmax())
        clusters = [(c_sum[j] / c_n[j], int(c_n[j]))]
    clusters.sort(key=lambda c: -c[1])
    return clusters


def _nearest_lut(pal_rgb):
    """For every 6-bit grid cell: index of nearest palette color (CIELAB) and
    whether it is within PURE_DE of it."""
    pal_lab = _srgb_to_lab(np.array(pal_rgb))
    d = ((_grid_lab()[:, None, :] - pal_lab[None]) ** 2).sum(-1)
    return d.argmin(1).astype(np.int32), np.sqrt(d.min(1)) <= PURE_DE


# Lab weights: JPEG keeps luma sharp but smears chroma, so halo colors share
# the lightness of a real color while their hue is off.
_LUMA_W = np.array([1.0, 0.35, 0.35], np.float32)


def _prune_fringes(rgb1x, mask1x, clusters, fringe_pct):
    """Drop small, thin clusters that are anti-aliasing blends or JPEG chroma
    halos. Solid small shapes (icons, arrows) are kept."""
    if fringe_pct <= 0 or len(clusters) < 3:
        return clusters
    total = int(mask1x.sum())
    lut, _ = _nearest_lut([c for c, _ in clusters])
    L = np.where(mask1x, lut[_grid_key(rgb1x)], -1)
    P = np.pad(L, 1, mode="edge")
    same = ((P[:-2, 1:-1] == L) & (P[2:, 1:-1] == L) & (P[1:-1, :-2] == L) & (P[1:-1, 2:] == L))
    interior = np.bincount(L[same & (L >= 0)], minlength=len(clusters)) / np.maximum(
        np.bincount(L[L >= 0], minlength=len(clusters)), 1)
    wlab = _srgb_to_lab(np.array([c for c, _ in clusters])) * _LUMA_W
    fringe_px = total * fringe_pct / 100.0
    near_cache = {}

    def near(j):  # pixels within 2 px of cluster j
        if j not in near_cache:
            m = Image.fromarray((L == j).astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(5))
            near_cache[j] = np.asarray(m) > 0
        return near_cache[j]

    kept = []
    for i, (col, cnt) in enumerate(clusters):
        if kept and cnt < fringe_px and interior[i] < 0.2:
            mi = L == i
            big = wlab[kept]
            d = np.sqrt(((big - wlab[i]) ** 2).sum(1))
            # (a) a tinted halo of an existing color it touches
            tint = any(d[a] < 20 and near(kept[a])[mi].mean() > 0.5 for a in range(len(kept)))
            if tint:
                continue
            # (b) a blend between two existing colors it sits between
            blend = False
            for a in range(len(kept)):
                for b in range(a + 1, len(kept)):
                    ab = big[b] - big[a]
                    l2 = float(ab @ ab)
                    if l2 < 100:
                        continue
                    t = float((wlab[i] - big[a]) @ ab) / l2
                    if not (0.05 < t < 0.95) or np.linalg.norm(big[a] + t * ab - wlab[i]) >= 22:
                        continue
                    if (near(kept[a])[mi] & near(kept[b])[mi]).mean() > 0.4:
                        blend = True
                        break
                if blend:
                    break
            if blend:
                continue
        kept.append(i)
    return [clusters[i] for i in kept]


def _reduce_colors(clusters, n):
    """Merge the most similar pair until at most n colors remain."""
    kept = list(clusters)
    while len(kept) > max(1, n):
        lab = _srgb_to_lab(np.array([c for c, _ in kept]))
        d = ((lab[:, None] - lab[None]) ** 2).sum(-1)
        np.fill_diagonal(d, np.inf)
        a, b = np.unravel_index(d.argmin(), d.shape)
        if kept[a][1] < kept[b][1]:
            a, b = b, a
        (ca, na), (cb, nb) = kept[a], kept[b]
        kept[a] = ((ca * na + cb * nb) / (na + nb), na + nb)
        kept.pop(b)
    return kept


_NEIGHBORS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _absorb_minor(lab: np.ndarray, unresolved: np.ndarray, src: np.ndarray,
                  pal: np.ndarray) -> None:
    """Assign unresolved pixels (blends, specks) to the nearest color among the
    colors present around them, growing inwards. Picking the nearest (not the
    most common) neighbour color puts edges at the true midpoint, so thin text
    strokes keep their width."""
    H, W = lab.shape
    K = len(pal)
    unresolved = unresolved.copy()
    for _ in range(64):
        ys, xs = np.nonzero(unresolved)
        if ys.size == 0:
            return
        nb = np.stack([lab[np.clip(ys + dy, 0, H - 1), np.clip(xs + dx, 0, W - 1)]
                       for dy, dx in _NEIGHBORS])
        cnt = np.stack([(nb == k).sum(0) for k in range(K)], 1)
        has = cnt.max(1) > 0
        if not has.any():
            break
        px = src[ys, xs].astype(np.float32)
        dist = ((px[:, None, :] - pal[None]) ** 2).sum(-1)
        best = ((cnt > 0) * 1e9 - dist).argmax(1)
        lab[ys[has], xs[has]] = best[has]
        unresolved[ys[has], xs[has]] = False
    ys, xs = np.nonzero(unresolved)
    if ys.size:
        px = src[ys, xs].astype(np.float32)
        lab[ys, xs] = ((px[:, None, :] - pal[None]) ** 2).sum(-1).argmin(1)


def _smooth_labels(lab: np.ndarray, n: int, sigma: float) -> np.ndarray:
    """Majority-smooth a label map (labels -1..n-1)."""
    if sigma <= 0:
        return lab
    blur = ImageFilter.GaussianBlur(sigma)
    best = np.full(lab.shape, -1, np.int16)
    out = np.full(lab.shape, -1, np.int32)
    for k in range(-1, n):
        m = lab == k
        if not m.any():
            continue
        p = np.asarray(Image.fromarray(m.astype(np.uint8) * 255).filter(blur), np.int16)
        win = p > best
        best[win] = p[win]
        out[win] = k
    return out


LOW_CONTRAST_DE = 10.0  # luma-weighted delta-E below which a color counts as noise
PURE_DE = 12.0  # pixels farther than this from every palette color go to neighbour vote


def _quantize(rgb1x, opaque1x, alpha1x, rgb, opaque, n, opts, scale):
    """Classify pixels into a perceptual palette.

    rgb1x/opaque1x: original-size image (palette is chosen here, fast);
    rgb/opaque: upscaled image (classified here).
    Returns (labels HxW, -1 = transparent; palette list[(r,g,b)])."""
    if not opaque1x.any():
        return np.full(opaque.shape, -1, np.int32), []
    # Sample colors only from fully opaque pixels: edge pixels that blend into
    # transparency would otherwise add fake tints.
    solid = opaque1x & (alpha1x >= 250)
    if not solid.any():
        solid = opaque1x
    fixed = parse_palette(opts.palette)
    if fixed:
        pal = [np.array(c, float) for c in fixed]
    else:
        clusters = _cluster_colors(rgb1x[solid], opts.merge_de)
        if opts.cleanup:
            clusters = _prune_fringes(rgb1x, solid, clusters, opts.merge_below)
        pal = [c for c, _ in _reduce_colors(clusters, n)]
    lut, lut_pure = _nearest_lut(pal)
    if fixed:  # every pixel snaps to the nearest given color
        lut_pure = np.ones_like(lut_pure)

    # Exact output colors: most frequent true color among pure pixels (1x).
    keys1 = _grid_key(rgb1x)
    lab1 = np.where(opaque1x, lut[keys1], -1)
    pure1 = lut_pure[keys1] & opaque1x
    palette = []
    for k in range(len(pal)):
        if fixed:
            palette.append(fixed[k])
            continue
        m = (lab1 == k) & pure1
        if not m.any():
            m = lab1 == k
        palette.append(_mode_color(rgb1x[m]) if m.any() else tuple(int(v) for v in pal[k]))

    keys = _grid_key(rgb)
    lab = np.where(opaque, lut[keys], -1).astype(np.int32)
    unresolved = opaque & ~lut_pure[keys] if opts.cleanup else np.zeros(opaque.shape, bool)
    if unresolved.any():
        lab[unresolved] = -2  # not a vote
        _absorb_minor(lab, unresolved, rgb, np.array(palette, np.float32))
    lab = _smooth_labels(lab, len(palette), opts.edge_smooth * scale)
    return lab, palette


# --------------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------------
def trace(img: Image.Image, opts: TraceOptions, cancelled=lambda: False) -> TraceResult:
    img = img.convert("RGBA")
    w, h = img.size
    scale = effective_scale(w, h, opts)

    alpha1 = img.getchannel("A")
    alpha1a = np.asarray(alpha1)
    opaque1 = (np.asarray(alpha1) >= 128) if opts.keep_transparency else np.ones((h, w), bool)
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    rgb1 = np.asarray(Image.alpha_composite(white, img).convert("RGB"))
    rgb1 = _bilateral(rgb1, opts.denoise)

    texts: list[TextItem] = []
    if opts.ocr and ocr_available():
        rgb1, texts = _apply_ocr(rgb1, opts)

    if opts.mode == "multi_grays":
        g = np.asarray(Image.fromarray(rgb1).convert("L"))
        rgb1 = np.repeat(g[..., None], 3, axis=2)
    if opts.smooth and opts.mode in ("multi_colors", "multi_grays"):
        rgb1 = np.asarray(Image.fromarray(rgb1).filter(ImageFilter.GaussianBlur(1.0)))

    if scale > 1:
        size = (w * scale, h * scale)
        rgb = None
        if opts.upscaler != "lanczos":
            rgb = _ai_upscale(rgb1, scale, opts.upscaler)
        if rgb is None:
            rgb = np.asarray(Image.fromarray(rgb1).resize(size, Image.Resampling.LANCZOS))
        else:
            # AI models shift colors slightly: sample the palette from the AI
            # output (box-downsampled) so classification matches it.
            rgb1 = np.asarray(Image.fromarray(rgb).resize((w, h), Image.Resampling.BOX))
        if opts.keep_transparency:
            opaque = np.asarray(alpha1.resize(size, Image.Resampling.LANCZOS)) >= 128
        else:
            opaque = np.ones((size[1], size[0]), bool)
    else:
        rgb, opaque = rgb1, opaque1

    layers: list[Layer] = []
    mode = opts.mode

    def tm(mask):
        return _trace_mask(mask, opts, scale)

    if mode == "brightness":
        mask = _luminance(rgb) < opts.threshold
        if opts.invert:
            mask = ~mask
        layers.append(Layer((0, 0, 0), tm(mask & opaque)))

    elif mode == "centerline":
        from centerline import trace_centerline
        mask = _luminance(rgb) < opts.threshold
        if opts.invert:
            mask = ~mask
        mask &= opaque
        contours, width = trace_centerline(mask, scale, min_length=max(2.0, opts.speckles * 2.0))
        ink = mask[::scale, ::scale] if scale > 1 else mask
        src = rgb1[ink[:rgb1.shape[0], :rgb1.shape[1]]] if ink.any() else None
        color = tuple(int(v) for v in np.median(src, axis=0)) if src is not None and len(src) \
            else (0, 0, 0)
        sw = opts.stroke_width if opts.stroke_width > 0 else max(0.5, width)
        layers.append(Layer(color, contours, stroke_width=sw))

    elif mode == "edge":
        mask = _edge_mask(rgb, opts.threshold, scale)
        if opts.invert:
            mask = ~mask
        layers.append(Layer((0, 0, 0), tm(mask & opaque)))

    elif mode == "quant":
        lab, palette = _quantize(rgb1, opaque1, alpha1a, rgb, opaque, opts.quant_colors, opts, scale)
        order = sorted(range(len(palette)), key=lambda i: sum(palette[i]))
        rank = np.zeros(len(palette) + 1, np.int32)
        for r, i in enumerate(order):
            rank[i] = r
        mask = ((rank[lab] % 2) == 0) & (lab >= 0)
        if opts.invert:
            mask = ~mask & opaque
        layers.append(Layer((0, 0, 0), tm(mask)))

    elif mode == "multi_brightness":
        lum = _luminance(rgb)
        n = max(1, opts.scans)
        thresholds = [(k + 1) / (n + 1) for k in range(n)]
        for k, t in reversed(list(enumerate(thresholds))):
            if cancelled():
                break
            lo = thresholds[k - 1] if k > 0 else 0.0
            mask = lum < t if opts.stack else (lum < t) & (lum >= lo)
            v = int(round(255 * (lo + t) / 2))
            layers.append(Layer((v, v, v), tm(mask & opaque)))
        if opts.remove_background and layers:
            layers.pop(0)

    elif mode in ("multi_colors", "multi_grays"):
        lab, palette = _quantize(rgb1, opaque1, alpha1a, rgb, opaque, opts.scans, opts, scale)
        counts = [int((lab == k).sum()) for k in range(len(palette))]
        # Most common color (background) first; when stacking each layer covers
        # itself and every later layer so no gaps appear between shapes.
        order = [k for k in sorted(range(len(palette)), key=lambda k: -counts[k]) if counts[k]]
        if opts.remove_background and len(order) > 1:
            order.pop(0)
        # Smart stacking: each shape is its own region grown by 1px (no hairline
        # gaps) with holes filled when later (higher) shapes cover them anyway.
        # Far fewer paths than classic union stacking, same gap-free look.
        below = ~opaque if opts.keep_transparency else np.zeros(opaque.shape, bool)
        if opts.remove_background and len(counts) and order != sorted(
                range(len(palette)), key=lambda k: -counts[k]):
            below = below | (lab == sorted(range(len(palette)), key=lambda k: -counts[k])[0])
        # Contrast-adaptive speckle filter: a tiny shape whose color is close to
        # a color already painted below (JPEG halo, shade noise) is dropped with
        # the bigger ``noise_speckles`` size; distinct colors (text, icons)
        # keep the fine ``speckles`` size so small letters survive.
        wlab = _srgb_to_lab(np.array(palette, np.float32)) * _LUMA_W if palette else None
        painted = []
        for k in order:
            if cancelled():
                break
            own = lab == k
            sp = opts.speckles
            if painted and opts.noise_speckles > opts.speckles:
                if np.sqrt(((wlab[painted] - wlab[k]) ** 2).sum(1)).min() < LOW_CONTRAST_DE:
                    sp = opts.noise_speckles
            painted.append(k)
            if opts.stack:
                mask = _dilate1(own) & opaque
                contours = _trace_mask(mask, opts, scale, keep_hole=_hole_checker(below),
                                       speckles=sp)
            else:
                contours = _trace_mask(own, opts, scale, speckles=sp)
            layers.append(Layer(palette[k], contours))
            below |= own
    else:
        raise ValueError(f"unknown mode {mode}")

    return TraceResult(w, h, layers, scale, texts, opts.font_family)
