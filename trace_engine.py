# SPDX-License-Identifier: GPL-3.0-or-later
"""Bitmap -> vector tracing engine (libpotrace via ctypes).

Modes (ids used by the GUI and presets):
  multi_colors      full color: perceptual palette, one layer per color
  multi_grays       the same in gray
  multi_brightness  evenly spaced light-to-dark layers ("gray steps")
  brightness        black & white threshold
  edge              outlines only
  centerline        line art -> single strokes (see centerline.py)
  quant             posterize: few colors alternated black/white

Pipeline (see ``trace``):
  1. edge-preserving denoise (bilateral)          - removes JPEG noise
  2. optional OCR (tesseract)                      - text becomes real SVG <text>
  3. upscale (Lanczos or an AI model)              - smooth curves, crisp small text
  4. perceptual palette in CIELAB                  - merges look-alike shades, keeps
                                                     small vivid colors
  5. blend-pixel assignment + region smoothing
  6. smart stacking + contrast-adaptive speckles   - gap-free, compact output
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Callable, NamedTuple
from xml.sax.saxutils import escape

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("image_tracer")

# --------------------------------------------------------------------------
# Tuning constants
# --------------------------------------------------------------------------
AUTO_TARGET = 2000          # auto-upscale small images until the long side is ~this
MAX_SCALE = 4               # upscale limit

PURE_DE = 12.0              # farther than this from every palette color -> blend pixel
LOW_CONTRAST_DE = 10.0      # luma-weighted delta-E below which a color counts as noise
LUMA_WEIGHTS = np.array([1.0, 0.35, 0.35], np.float32)  # JPEG keeps luma, smears chroma

CLUSTER_MIN_PX = 8          # clusters smaller than this ...
CLUSTER_MIN_SHARE = 0.0002  # ... or this share of the image are dropped

FRINGE_MAX_INTERIOR = 0.2   # thin clusters (few interior pixels) may be fringes
FRINGE_TINT_DE = 20.0       # "tinted halo" of a neighbouring color
FRINGE_TINT_NEAR = 0.5      #   ... share of pixels that must touch that color
FRINGE_BLEND_DE = 22.0      # distance from the line between two colors
FRINGE_BLEND_NEAR = 0.4     #   ... share of pixels that must touch both colors

HOLE_MIN_HITS = 2           # a hole is kept only if it reveals this many pixels ...
HOLE_MIN_SHARE = 0.02       # ... and this share of its area

OCR_TARGET = 2400           # OCR input is upscaled until the long side is ~this
OCR_LOW_CONF = 30.0         # numbers / known words are accepted down to this confidence
OCR_MIN_BOX_H = 5           # px; smaller "words" are noise
OCR_OVERLAP = 0.4           # duplicate detection between the two polarities
OCR_MIN_CONTRAST = 40.0     # RGB distance text <-> background
OCR_MIN_LUMA_RANGE = 15.0   # luminance range needed to estimate coverage
OCR_INK_COVERAGE = 0.35     # coverage counted as ink when measuring text extent
UNIT_GAP_EM = 0.22          # empty run (in text heights) that counts as a word space

# Typical font metrics as a fraction of the em size.
CAP_HEIGHT, X_HEIGHT, DESCENDER = 0.73, 0.53, 0.22
_ASC = set("bdfhklt()[]{}/\\|!?%#@&$'\"")
_DESC = set("gjpqy,;()[]{}|_")

DEFAULT_FONT = "Segoe UI, Noto Sans, Arial, sans-serif"
MODE_IDS = ("multi_colors", "brightness", "centerline", "edge", "multi_grays",
            "multi_brightness", "quant")


class Cancelled(Exception):
    """Raised inside ``trace`` when the caller's ``cancelled()`` turns true."""


# --------------------------------------------------------------------------
# libpotrace bindings
# --------------------------------------------------------------------------
import sys  # noqa: E402

# Folder of a frozen (PyInstaller) build, where bundled libraries/tools live.
BUNDLE_DIR = getattr(sys, "_MEIPASS", None)


def _load_potrace():
    """libpotrace from a bundle, the system library path, or its usual names
    on Linux, Windows (MSYS2) and macOS (Homebrew)."""
    names = ["libpotrace.so.0", "libpotrace-0.dll", "libpotrace.0.dylib", "libpotrace.dylib"]
    candidates = []
    if BUNDLE_DIR:
        candidates += [os.path.join(BUNDLE_DIR, n) for n in names]
    found = ctypes.util.find_library("potrace")
    if found:
        candidates.append(found)
    candidates += names + ["/opt/homebrew/lib/libpotrace.dylib", "/usr/local/lib/libpotrace.dylib"]
    errors = []
    for c in candidates:
        try:
            return ctypes.CDLL(c)
        except OSError as e:
            errors.append(f"{c}: {e}")
    raise OSError("libpotrace not found:\n  " + "\n  ".join(errors))


_lib = _load_potrace()


def _tool(name: str) -> str | None:
    """Path of an external tool: bundled copy first, then PATH."""
    if BUNDLE_DIR:
        for cand in (name, name + ".exe"):
            p = os.path.join(BUNDLE_DIR, "bin", cand)
            if os.path.isfile(p):
                return p
    return shutil.which(name)


def _tesseract_env() -> dict | None:
    """Environment for a bundled tesseract (points it at the bundled data)."""
    if BUNDLE_DIR and os.path.isdir(os.path.join(BUNDLE_DIR, "tessdata")):
        return {**os.environ, "TESSDATA_PREFIX": os.path.join(BUNDLE_DIR, "tessdata")}
    return None

POTRACE_CURVETO = 1
POTRACE_CORNER = 2
POTRACE_SIGN_HOLE = ord("-")
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
# Small thread-safe helpers
# --------------------------------------------------------------------------
class _Memo:
    """Single-entry memo, safe to use from several trace threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self._entry = None

    def get(self, key):
        with self._lock:
            entry = self._entry
        return entry[1] if entry is not None and entry[0] == key else None

    def put(self, key, value):
        with self._lock:
            self._entry = (key, value)


def _digest(arr: np.ndarray) -> tuple:
    """Content key for an image array (full hash, not a sample)."""
    a = np.ascontiguousarray(arr)
    return a.shape, a.dtype.str, hashlib.blake2b(a.tobytes(), digest_size=16).digest()


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
class Contour(NamedTuple):
    start: tuple[float, float]
    segs: list  # ("C", (x1,y1,x2,y2,x,y)) | ("L", (x,y))
    closed: bool = True


@dataclass
class Layer:
    color: tuple[int, int, int]
    contours: list[Contour] = field(default_factory=list)
    stroke_width: float = 0.0  # > 0: centerline strokes instead of fills

    def svg_d(self, precision: int = 1) -> str:
        """Compact path data: relative commands and few decimals (coordinates
        are original-image pixels, so 0.1 px is plenty)."""
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
            sx, sy = c.start
            cx, cy = round(sx * q) / q, round(sy * q) / q
            parts.append("M" + join((cx, cy)))
            for kind, pts in c.segs:
                rel = []
                for i in range(0, len(pts), 2):
                    rel += [pts[i] - cx, pts[i + 1] - cy]
                # accumulate from rounded values so errors don't drift
                rel = [round(v * q) / q for v in rel]
                cx, cy = cx + rel[-2], cy + rel[-1]
                parts.append(kind.lower() + join(rel))
            if c.closed:
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


_FONTS: dict = {}
_FONTS_LOCK = threading.Lock()


def _font_for(family: str):
    """PIL font for the face this machine resolves ``family`` to (cached)."""
    first = family.split(",")[0].strip().strip("'\"") or "sans-serif"
    with _FONTS_LOCK:
        if first in _FONTS:
            return _FONTS[first]
    font = None
    try:
        fc = _tool("fc-match")
        if not fc:
            raise FileNotFoundError("fc-match")
        path = subprocess.run([fc, "-f", "%{file}", first], capture_output=True,
                              text=True, timeout=10).stdout.strip()
        if path:
            font = ImageFont.truetype(path, 100)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("cannot resolve font %r: %s", first, e)
    with _FONTS_LOCK:
        _FONTS[first] = font
    return font


def _measure(family: str, size: float, text: str) -> float | None:
    font = _font_for(family)
    return font.getlength(text) * size / 100.0 if font else None


def _hex(color) -> str:
    r, g, b = color
    return f"#{r:02x}{g:02x}{b:02x}"


@dataclass
class TraceResult:
    width: int
    height: int
    layers: list[Layer]
    scale: int = 1
    texts: list[TextItem] = field(default_factory=list)
    font_family: str = DEFAULT_FONT
    warnings: list[str] = field(default_factory=list)

    @property
    def path_count(self) -> int:
        return sum(len(layer.contours) for layer in self.layers)

    @property
    def node_count(self) -> int:
        return sum(len(c.segs) for layer in self.layers for c in layer.contours)

    def to_svg(self) -> str:
        out = [
            '<?xml version="1.0" encoding="UTF-8"?>\n',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" '
            f'height="{self.height}" viewBox="0 0 {self.width} {self.height}">\n',
        ]
        for layer in self.layers:
            if not layer.contours:
                continue
            if layer.stroke_width > 0:
                out.append(f'  <path fill="none" stroke="{_hex(layer.color)}" '
                           f'stroke-width="{layer.stroke_width:.2f}" stroke-linecap="round" '
                           f'stroke-linejoin="round" d="{layer.svg_d()}"/>\n')
            else:
                out.append(f'  <path fill="{_hex(layer.color)}" d="{layer.svg_d()}"/>\n')
        if self.texts:
            out.append(f'  <g font-family="{escape(self.font_family)}">\n')
            for t in self.texts:
                # letter-spacing makes the width right where textLength is not
                # supported (e.g. librsvg); textLength fine-tunes in browsers.
                nat = _measure(self.font_family, t.size, t.text)
                ls = ""
                if nat and len(t.text) > 1:
                    ls = f' letter-spacing="{(t.width - nat) / len(t.text):.2f}"'
                out.append(
                    f'    <text x="{t.x:.2f}" y="{t.y:.2f}" font-size="{t.size:.2f}"{ls} '
                    f'textLength="{t.width:.2f}" lengthAdjust="spacingAndGlyphs" '
                    f'fill="{_hex(t.color)}" xml:space="preserve">{escape(t.text)}</text>\n')
            out.append("  </g>\n")
        out.append("</svg>\n")
        return "".join(out)


@dataclass
class TraceOptions:
    mode: str = "multi_colors"  # one of MODE_IDS
    threshold: float = 0.45  # brightness cutoff / edge threshold (0..1)
    quant_colors: int = 8  # posterize color count
    scans: int = 8  # max colors / gray steps
    invert: bool = False
    smooth: bool = False  # blur before multi scans
    stack: bool = True  # stack layers (no gaps)
    remove_background: bool = False
    # potrace options
    speckles: int = 2  # turdsize, in original-image pixels
    smooth_corners: float = 1.0  # alphamax
    optimize: float = 0.2  # opttolerance
    optimize_enabled: bool = True
    # quality options
    upscale: int = 0  # 0 = auto, otherwise 1..MAX_SCALE
    merge_below: float = 1.0  # anti-aliasing colors under this % of the image are dropped
    merge_de: float = 8.0  # colors closer than this (CIELAB delta-E) are merged
    keep_transparency: bool = True
    edge_smooth: float = 1.0  # smooth color-region boundaries (original px)
    denoise: float = 0.0  # edge-preserving denoise strength (0 = off)
    cleanup: bool = True  # remove anti-aliasing/halo colors (logos); off = faithful
    noise_speckles: int = 6  # speckle size for low-contrast colors (halos, shade noise)
    palette: str = ""  # fixed palette "#112233, #445566" (empty = automatic)
    upscaler: str = "lanczos"  # lanczos | a name from ai_upscalers()
    stroke_width: float = 0.0  # centerline: 0 = auto
    # OCR
    ocr: bool = False
    ocr_min_conf: float = 70.0
    font_family: str = DEFAULT_FONT


def parse_palette(text: str) -> list[tuple[int, int, int]]:
    """'#1b1b1b, e0c088 #fff' -> [(27,27,27), (224,192,136), (255,255,255)]"""
    out = []
    for tok in re.findall(r"(?<![0-9a-zA-Z])#?([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])",
                          text or ""):
        if len(tok) == 3:
            tok = "".join(ch * 2 for ch in tok)
        out.append(tuple(int(tok[i:i + 2], 16) for i in (0, 2, 4)))
    return out


def effective_scale(w: int, h: int, opts: TraceOptions) -> int:
    if opts.upscale:
        return max(1, min(MAX_SCALE, int(opts.upscale)))
    return max(1, min(MAX_SCALE, AUTO_TARGET // max(w, h, 1)))


# --------------------------------------------------------------------------
# AI upscalers (ncnn/Vulkan command-line tools)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class _AIBackend:
    binary: str
    scales: tuple[int, ...]  # native scale factors
    args: Callable[[str, str, int], list[str]]
    model_dirs: tuple[str, ...] = ()  # packaged model folders, used if present


_AI_BACKENDS = {
    "Real-ESRGAN": _AIBackend(
        "realesrgan-ncnn-vulkan", (4,),
        lambda i, o, s: ["-i", i, "-o", o, "-s", "4", "-n", "realesrgan-x4plus"],
        ("/usr/share/realesrgan-ncnn-vulkan/models", "/usr/share/realesrgan-ncnn-vulkan")),
    "Real-CUGAN": _AIBackend(
        "realcugan-ncnn-vulkan", (2, 3, 4),
        lambda i, o, s: ["-i", i, "-o", o, "-s", str(s), "-n", "0"],
        ("/usr/share/realcugan-ncnn-vulkan/models-se",)),
    "waifu2x": _AIBackend(
        "waifu2x-ncnn-vulkan", (2, 4),
        lambda i, o, s: ["-i", i, "-o", o, "-s", str(s), "-n", "1"],
        ("/usr/share/waifu2x-ncnn-vulkan/models-cunet",)),
}
_AI_MEMO = _Memo()


def ai_upscalers() -> list[str]:
    return [name for name, b in _AI_BACKENDS.items() if shutil.which(b.binary)]


def _ai_upscale(rgb: np.ndarray, scale: int, name: str) -> tuple[np.ndarray | None, str | None]:
    """Upscale with an AI model. Returns (image, None) or (None, reason)."""
    backend = _AI_BACKENDS.get(name)
    if backend is None or not shutil.which(backend.binary):
        return None, f"AI upscaler {name!r} is not installed; used Lanczos"
    key = (name, scale, _digest(rgb))
    cached = _AI_MEMO.get(key)
    if cached is not None:
        return cached, None
    native = min((n for n in backend.scales if n >= scale), default=max(backend.scales))
    tmp = tempfile.mkdtemp(prefix="imgtrace-ai-")
    try:
        src, dst = os.path.join(tmp, "in.png"), os.path.join(tmp, "out.png")
        Image.fromarray(rgb).save(src)
        cmd = [backend.binary] + backend.args(src, dst, native)
        model_dir = next((d for d in backend.model_dirs if os.path.isdir(d)), None)
        if model_dir:
            cmd += ["-m", model_dir]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if not os.path.exists(dst):
            err = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
            raise RuntimeError(err[0])
        out = Image.open(dst).convert("RGB")
        h, w = rgb.shape[:2]
        if out.size != (w * scale, h * scale):
            out = out.resize((w * scale, h * scale), Image.Resampling.LANCZOS)
        res = np.asarray(out)
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        log.warning("AI upscaler %s failed: %s", name, e)
        return None, f"AI upscaler {name} failed ({e}); used Lanczos"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    _AI_MEMO.put(key, res)
    return res, None


# --------------------------------------------------------------------------
# OCR availability
# --------------------------------------------------------------------------
_OCR_STATUS: tuple[bool, str] | None = None


def ocr_status() -> tuple[bool, str]:
    """(usable, message). Needs the tesseract binary and English data.
    A negative answer is re-checked each call, so installing works live."""
    global _OCR_STATUS
    if _OCR_STATUS is None or not _OCR_STATUS[0]:
        if not _tool("tesseract"):
            _OCR_STATUS = (False, "OCR needs tesseract: sudo pacman -S tesseract tesseract-data-eng")
        else:
            try:
                langs = subprocess.run([_tool("tesseract"), "--list-langs"], capture_output=True,
                                       text=True, timeout=20, env=_tesseract_env()).stdout.split()
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("tesseract --list-langs failed: %s", e)
                langs = []
            _OCR_STATUS = ((True, "OCR ready") if "eng" in langs else
                           (False, "OCR needs English data: sudo pacman -S tesseract-data-eng"))
    return _OCR_STATUS


def ocr_available() -> bool:
    return ocr_status()[0]


# --------------------------------------------------------------------------
# potrace wrapper
# --------------------------------------------------------------------------
def _trace_mask(mask: np.ndarray, opts: TraceOptions, scale: int = 1,
                keep_hole=None, speckles: int | None = None) -> list[Contour]:
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

    def pt(p):
        return (p.x + x0) * k, (p.y + y0) * k

    contours = []
    try:
        node = state.contents.plist
        while node:
            pth = node.contents
            n, tags, c = pth.curve.n, pth.curve.tag, pth.curve.c
            if keep_hole is not None and pth.sign == POTRACE_SIGN_HOLE:
                outline = np.array([(c[i][2].x + x0, c[i][2].y + y0) for i in range(n)])
                if not keep_hole(outline):
                    node = pth.next
                    continue
            segs = []
            for i in range(n):
                seg = c[i]
                if tags[i] == POTRACE_CORNER:
                    segs.append(("L", pt(seg[1])))
                    segs.append(("L", pt(seg[2])))
                else:
                    segs.append(("C", pt(seg[0]) + pt(seg[1]) + pt(seg[2])))
            contours.append(Contour(pt(c[n - 1][2]), segs))
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
        return hits > max(HOLE_MIN_HITS, HOLE_MIN_SHARE * int(inside.sum()))

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


_GRID_LAB = None  # CIELAB of a 64^3 RGB grid, for fast pixel classification
_GRID_LOCK = threading.Lock()


def _grid_lab():
    global _GRID_LAB
    with _GRID_LOCK:
        if _GRID_LAB is None:
            v = np.arange(64, dtype=np.float32) * 4 + 2
            r, g, b = np.meshgrid(v, v, v, indexing="ij")
            _GRID_LAB = _srgb_to_lab(np.stack([r, g, b], -1).reshape(-1, 3))
    return _GRID_LAB


def _grid_key(rgb: np.ndarray) -> np.ndarray:
    q = rgb.astype(np.int32) >> 2
    return (q[..., 0] << 12) | (q[..., 1] << 6) | q[..., 2]


def _luminance(rgb: np.ndarray) -> np.ndarray:
    rgb = rgb.astype(np.float32)
    return (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]) / 255.0


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


# --------------------------------------------------------------------------
# OCR (tesseract)
# --------------------------------------------------------------------------
class OcrWord(NamedTuple):
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    conf: float

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return (self.x1 - self.x0) * (self.y1 - self.y0)


class _TextLine(NamedTuple):
    x0: float
    y0: float
    x1: float
    y1: float
    text: str


_OCR_MEMO = _Memo()


def _run_tesseract(gray: Image.Image, factor: int) -> list[OcrWord]:
    """Words in original coordinates (input was upscaled by ``factor``)."""
    tmp = tempfile.mkdtemp(prefix="imgtrace-")
    try:
        path = os.path.join(tmp, "in.png")
        gray.save(path)
        proc = subprocess.run([_tool("tesseract"), path, "stdout", "--psm", "11", "tsv"],
                              capture_output=True, text=True, timeout=120, env=_tesseract_env())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        log.warning("tesseract failed: %s", proc.stderr.strip()[-200:])
    words = []
    for line in proc.stdout.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 12 or cols[0] != "5":  # level 5 = word
            continue
        text = cols[11].strip()
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if not text or conf < 0:
            continue
        x, y, w, h = (int(v) / factor for v in cols[6:10])
        words.append(OcrWord(x, y, x + w, y + h, text, conf))
    return words


def _ocr_words(rgb1x: np.ndarray, min_conf: float) -> list[OcrWord]:
    key = (_digest(rgb1x), min_conf)
    cached = _OCR_MEMO.get(key)
    if cached is not None:
        return cached
    h, w = rgb1x.shape[:2]
    factor = max(1, min(4, OCR_TARGET // max(w, h, 1)))
    gray = Image.fromarray(rgb1x).convert("L")
    if factor > 1:
        gray = gray.resize((w * factor, h * factor), Image.Resampling.LANCZOS)
    inv = Image.fromarray(255 - np.asarray(gray))
    # Both polarities: light-on-dark and dark-on-light text.
    cands = _run_tesseract(gray, factor) + _run_tesseract(inv, factor)
    # Confident words, plus low-confidence ones that are numbers or repeat a word
    # read confidently elsewhere (dim table cells and the like).
    vocab = {c.text for c in cands if c.conf >= min_conf}
    numeric = re.compile(r"^[0-9][0-9.,:%/-]*$")
    cands = [c for c in cands
             if any(ch.isalnum() for ch in c.text) and c.height >= OCR_MIN_BOX_H
             and (c.conf >= min_conf
                  or (c.conf >= OCR_LOW_CONF and (c.text in vocab or numeric.match(c.text))))]
    cands.sort(key=lambda c: -c.conf)
    words: list[OcrWord] = []
    for c in cands:
        for d in words:
            ix = min(c.x1, d.x1) - max(c.x0, d.x0)
            iy = min(c.y1, d.y1) - max(c.y0, d.y0)
            if ix > 0 and iy > 0 and ix * iy > OCR_OVERLAP * min(c.area, d.area):
                break  # same word from the other polarity
        else:
            words.append(c)
    _OCR_MEMO.put(key, words)
    return words


def _clean_words(words: list[OcrWord]) -> list[OcrWord]:
    """Drop icons that OCR misreads as 1-2 character 'words' ('2', 'DI', 'O',
    'X'). A short token is kept when it follows a word ("0.1 MB", "go to") or
    precedes another short token ("0 MB"); it is dropped when it stands alone
    or sits right before a longer label (an icon in front of its text)."""
    def same_line(a, b):
        return abs((a.y0 + a.y1) - (b.y0 + b.y1)) < max(a.height, b.height)

    out = []
    for w in words:
        if len(w.text) > 2:
            out.append(w)
            continue

        def gap(v):
            return 0.9 * max(w.height, v.height, 6)

        others = [v for v in words if v is not w and same_line(v, w)]
        left = [v for v in others if 0 <= w.x0 - v.x1 <= gap(v)]
        right = [v for v in others if 0 <= v.x0 - w.x1 <= gap(v)]
        if left or (right and min(len(v.text) for v in right) <= 2):
            out.append(w)
    return out


def _group_lines(words: list[OcrWord]) -> list[_TextLine]:
    """Group words into text lines (x-height-only words join cap-height ones)."""
    lines = []  # [x0, y0, x1, y1, [texts]]
    for w in sorted(words, key=lambda c: c.x0):
        h = w.height
        for ln in lines:
            lh = ln[3] - ln[1]
            ov = min(w.y1, ln[3]) - max(w.y0, ln[1])
            gap = w.x0 - ln[2]
            if ov > 0.5 * min(h, lh) and 0.4 < h / lh < 2.5 and -2 <= gap <= 0.9 * max(h, lh):
                ln[0], ln[1] = min(ln[0], w.x0), min(ln[1], w.y0)
                ln[2], ln[3] = max(ln[2], w.x1), max(ln[3], w.y1)
                ln[4].append(w.text)
                break
        else:
            lines.append([w.x0, w.y0, w.x1, w.y1, [w.text]])
    return [_TextLine(a, b, c, d, " ".join(t)) for a, b, c, d, t in lines]


def _fix_unit_spaces(text: str, ink: np.ndarray | None) -> str:
    """'0MB' -> '0 MB' when the pixels show a word-sized gap between the
    number and the unit (OCR often merges them)."""
    if ink is None or not ink.any() or not re.search(r"\d[A-Za-z]", text):
        return text
    h = int(np.ptp(np.flatnonzero(ink.any(1)))) + 1
    cols = ink.any(0)
    nz = np.flatnonzero(cols)
    runs, cur = [], 0
    for c in cols[nz[0]:nz[-1] + 1]:  # empty-column runs inside the ink extent
        if not c:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if sum(1 for r in runs if r >= UNIT_GAP_EM * h) > text.count(" "):
        text = re.sub(r"(\d)([A-Za-z]{1,3})\b", r"\1 \2", text)
    return text


def _apply_ocr(rgb1x: np.ndarray, opts: TraceOptions):
    """Detect text, return (rgb with text painted out, [TextItem])."""
    lines = _group_lines(_clean_words(_ocr_words(rgb1x, opts.ocr_min_conf)))
    out = rgb1x.copy()
    H, W = out.shape[:2]
    items = []
    for line in lines:
        x0, y0, x1, y1, text = line
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
        if np.sqrt(((box - bg) ** 2).sum(-1)).max() < OCR_MIN_CONTRAST:
            continue
        # Text color by unmixing: each pixel = a*fg + (1-a)*bg. Coverage a comes
        # from luminance (kept sharp by JPEG); summing (p - bg) over the box and
        # dividing by the summed coverage recovers the true color even though
        # JPEG smears chroma into halos.
        dl = _luminance(box) * 255 - float(_luminance(bg[None])[0] * 255)
        sgn = 1.0 if dl.max() >= -dl.min() else -1.0
        span = float(np.percentile(dl * sgn, 97))
        if span < OCR_MIN_LUMA_RANGE:
            continue
        a = np.clip(dl * sgn / span, 0, 1)
        fg = np.clip(bg + (box - bg).sum(0) / max(a.sum(), 1e-6), 0, 255)
        # Measure the actual ink (tesseract boxes can include borders).
        ink = a.reshape(iy1 - iy0, ix1 - ix0) > OCR_INK_COVERAGE
        rows, cols = np.flatnonzero(ink.any(1)), np.flatnonzero(ink.any(0))
        if rows.size and cols.size:
            y0, y1 = iy0 + rows[0], iy0 + rows[-1] + 1
            x0, x1 = ix0 + cols[0], ix0 + cols[-1] + 1
        else:
            ink = None
        out[iy0:iy1, ix0:ix1] = bg.astype(np.uint8)

        text = _fix_unit_spaces(text, ink)
        top = CAP_HEIGHT if any(ch in _ASC or ch.isupper() or ch.isdigit() for ch in text) \
            else X_HEIGHT
        bottom = DESCENDER if any(ch in _DESC for ch in text) else 0.0
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
    min_px = max(CLUSTER_MIN_PX, total * CLUSTER_MIN_SHARE)
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


def _prune_fringes(rgb1x, mask1x, clusters, fringe_pct):
    """Drop small, thin clusters that are anti-aliasing blends or JPEG chroma
    halos. Solid small shapes (icons, arrows) are kept."""
    if fringe_pct <= 0 or len(clusters) < 3:
        return clusters
    total = int(mask1x.sum())
    lut, _ = _nearest_lut([c for c, _ in clusters])
    L = np.where(mask1x, lut[_grid_key(rgb1x)], -1)
    P = np.pad(L, 1, mode="edge")
    same = (P[:-2, 1:-1] == L) & (P[2:, 1:-1] == L) & (P[1:-1, :-2] == L) & (P[1:-1, 2:] == L)
    interior = np.bincount(L[same & (L >= 0)], minlength=len(clusters)) / np.maximum(
        np.bincount(L[L >= 0], minlength=len(clusters)), 1)
    wlab = _srgb_to_lab(np.array([c for c, _ in clusters])) * LUMA_WEIGHTS
    fringe_px = total * fringe_pct / 100.0
    near_cache = {}

    def near(j):  # pixels within 2 px of cluster j
        if j not in near_cache:
            m = Image.fromarray((L == j).astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(5))
            near_cache[j] = np.asarray(m) > 0
        return near_cache[j]

    def is_tint(i, mi, d):
        return any(d[a] < FRINGE_TINT_DE and near(kept[a])[mi].mean() > FRINGE_TINT_NEAR
                   for a in range(len(kept)))

    def is_blend(i, mi, big):
        for a in range(len(kept)):
            for b in range(a + 1, len(kept)):
                ab = big[b] - big[a]
                l2 = float(ab @ ab)
                if l2 < 100:
                    continue
                t = float((wlab[i] - big[a]) @ ab) / l2
                if not (0.05 < t < 0.95) or np.linalg.norm(big[a] + t * ab - wlab[i]) >= FRINGE_BLEND_DE:
                    continue
                if (near(kept[a])[mi] & near(kept[b])[mi]).mean() > FRINGE_BLEND_NEAR:
                    return True
        return False

    kept = []
    for i, (_col, cnt) in enumerate(clusters):
        if kept and cnt < fringe_px and interior[i] < FRINGE_MAX_INTERIOR:
            mi = L == i
            big = wlab[kept]
            d = np.sqrt(((big - wlab[i]) ** 2).sum(1))
            if is_tint(i, mi, d) or is_blend(i, mi, big):
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


# --------------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------------
@dataclass
class _Ctx:
    """Prepared image data shared by the mode functions."""
    opts: TraceOptions
    scale: int
    rgb1: np.ndarray  # original size, denoised / text removed
    opaque1: np.ndarray
    alpha1: np.ndarray
    rgb: np.ndarray  # upscaled
    opaque: np.ndarray
    check: Callable[[], None]  # raises Cancelled

    def trace(self, mask, **kw) -> list[Contour]:
        return _trace_mask(mask, self.opts, self.scale, **kw)

    def quantize(self, n):
        return _quantize(self, n)


def _quantize(ctx: _Ctx, n: int):
    """Classify pixels into a perceptual palette. The palette is chosen on the
    original-size image (fast); pixels are classified on the upscaled one.
    Returns (labels HxW with -1 = transparent, palette list[(r,g,b)])."""
    opts, rgb1x, opaque1x, rgb, opaque = ctx.opts, ctx.rgb1, ctx.opaque1, ctx.rgb, ctx.opaque
    if not opaque1x.any():
        return np.full(opaque.shape, -1, np.int32), []
    # Sample colors only from fully opaque pixels: edge pixels that blend into
    # transparency would otherwise add fake tints.
    solid = opaque1x & (ctx.alpha1 >= 250)
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
    ctx.check()

    if fixed:
        palette = list(fixed)
    else:
        # Exact output colors: most frequent true color among pure pixels.
        keys1 = _grid_key(rgb1x)
        lab1 = np.where(opaque1x, lut[keys1], -1)
        pure1 = lut_pure[keys1] & opaque1x
        palette = []
        for k in range(len(pal)):
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
    ctx.check()
    lab = _smooth_labels(lab, len(palette), opts.edge_smooth * ctx.scale)
    return lab, palette


def _mode_brightness(ctx: _Ctx) -> list[Layer]:
    mask = _luminance(ctx.rgb) < ctx.opts.threshold
    if ctx.opts.invert:
        mask = ~mask
    return [Layer((0, 0, 0), ctx.trace(mask & ctx.opaque))]


def _mode_edge(ctx: _Ctx) -> list[Layer]:
    mask = _edge_mask(ctx.rgb, ctx.opts.threshold, ctx.scale)
    if ctx.opts.invert:
        mask = ~mask
    return [Layer((0, 0, 0), ctx.trace(mask & ctx.opaque))]


def _mode_centerline(ctx: _Ctx) -> list[Layer]:
    from centerline import trace_centerline
    opts, scale = ctx.opts, ctx.scale
    mask = _luminance(ctx.rgb) < opts.threshold
    if opts.invert:
        mask = ~mask
    mask &= ctx.opaque
    contours, width = trace_centerline(mask, scale, min_length=max(2.0, opts.speckles * 2.0))
    ink = mask[::scale, ::scale][:ctx.rgb1.shape[0], :ctx.rgb1.shape[1]]
    color = tuple(int(v) for v in np.median(ctx.rgb1[ink], axis=0)) if ink.any() else (0, 0, 0)
    sw = opts.stroke_width if opts.stroke_width > 0 else max(0.5, width)
    return [Layer(color, contours, stroke_width=sw)]


def _mode_quant(ctx: _Ctx) -> list[Layer]:
    lab, palette = ctx.quantize(ctx.opts.quant_colors)
    order = sorted(range(len(palette)), key=lambda i: sum(palette[i]))
    rank = np.zeros(len(palette) + 1, np.int32)  # last slot catches label -1
    for r, i in enumerate(order):
        rank[i] = r
    mask = ((rank[lab] % 2) == 0) & (lab >= 0)
    if ctx.opts.invert:
        mask = ~mask & ctx.opaque
    return [Layer((0, 0, 0), ctx.trace(mask))]


def _mode_gray_steps(ctx: _Ctx) -> list[Layer]:
    opts = ctx.opts
    lum = _luminance(ctx.rgb)
    n = max(1, opts.scans)
    thresholds = [(k + 1) / (n + 1) for k in range(n)]
    layers = []
    for k, t in reversed(list(enumerate(thresholds))):  # light first, dark on top
        ctx.check()
        lo = thresholds[k - 1] if k > 0 else 0.0
        mask = lum < t if opts.stack else (lum < t) & (lum >= lo)
        v = int(round(255 * (lo + t) / 2))
        layers.append(Layer((v, v, v), ctx.trace(mask & ctx.opaque)))
    if opts.remove_background and layers:
        layers.pop(0)
    return layers


def _mode_colors(ctx: _Ctx) -> list[Layer]:
    opts = ctx.opts
    lab, palette = ctx.quantize(opts.scans)
    counts = [int((lab == k).sum()) for k in range(len(palette))]
    # Most common color (background) first, smaller shapes on top.
    order = [k for k in sorted(range(len(palette)), key=lambda k: -counts[k]) if counts[k]]
    below = ~ctx.opaque if opts.keep_transparency else np.zeros(ctx.opaque.shape, bool)
    if opts.remove_background and len(order) > 1:
        below = below | (lab == order.pop(0))  # dropped background shows through
    # Smart stacking: each shape is its own region grown by 1px (no hairline
    # gaps) with holes filled when later shapes cover them anyway.
    # Contrast-adaptive speckles: a tiny shape whose color is close to one
    # already painted below (JPEG halo, shade noise) uses ``noise_speckles``;
    # distinct colors (text, icons) keep ``speckles`` so small letters survive.
    wlab = _srgb_to_lab(np.array(palette, np.float32)) * LUMA_WEIGHTS if palette else None
    painted, layers = [], []
    for k in order:
        ctx.check()
        own = lab == k
        sp = opts.speckles
        if painted and opts.noise_speckles > opts.speckles:
            if np.sqrt(((wlab[painted] - wlab[k]) ** 2).sum(1)).min() < LOW_CONTRAST_DE:
                sp = opts.noise_speckles
        painted.append(k)
        if opts.stack:
            contours = ctx.trace(_dilate1(own) & ctx.opaque, keep_hole=_hole_checker(below),
                                 speckles=sp)
        else:
            contours = ctx.trace(own, speckles=sp)
        layers.append(Layer(palette[k], contours))
        below |= own
    return layers


_MODE_FUNCS: dict[str, Callable[[_Ctx], list[Layer]]] = {
    "brightness": _mode_brightness,
    "edge": _mode_edge,
    "centerline": _mode_centerline,
    "quant": _mode_quant,
    "multi_brightness": _mode_gray_steps,
    "multi_colors": _mode_colors,
    "multi_grays": _mode_colors,
}
assert set(_MODE_FUNCS) == set(MODE_IDS)


def trace(img: Image.Image, opts: TraceOptions,
          cancelled: Callable[[], bool] = lambda: False) -> TraceResult:
    """Trace ``img`` with ``opts``. Raises Cancelled as soon as ``cancelled()``
    returns true (checked between stages and layers)."""
    if opts.mode not in _MODE_FUNCS:
        raise ValueError(f"unknown mode {opts.mode!r}")

    def check():
        if cancelled():
            raise Cancelled()

    img = img.convert("RGBA")
    w, h = img.size
    scale = effective_scale(w, h, opts)
    warnings: list[str] = []

    alpha_img = img.getchannel("A")
    alpha1 = np.asarray(alpha_img)
    opaque1 = alpha1 >= 128 if opts.keep_transparency else np.ones((h, w), bool)
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    rgb1 = np.asarray(Image.alpha_composite(white, img).convert("RGB"))
    rgb1 = _bilateral(rgb1, opts.denoise)
    check()

    texts: list[TextItem] = []
    if opts.ocr:
        if ocr_available():
            rgb1, texts = _apply_ocr(rgb1, opts)
            check()
        else:
            warnings.append(ocr_status()[1])

    if opts.mode == "multi_grays":
        g = np.asarray(Image.fromarray(rgb1).convert("L"))
        rgb1 = np.repeat(g[..., None], 3, axis=2)
    if opts.smooth and opts.mode in ("multi_colors", "multi_grays"):
        rgb1 = np.asarray(Image.fromarray(rgb1).filter(ImageFilter.GaussianBlur(1.0)))

    if scale > 1:
        size = (w * scale, h * scale)
        rgb = None
        if opts.upscaler != "lanczos":
            rgb, warning = _ai_upscale(rgb1, scale, opts.upscaler)
            if warning:
                warnings.append(warning)
        if rgb is None:
            rgb = np.asarray(Image.fromarray(rgb1).resize(size, Image.Resampling.LANCZOS))
        else:
            # AI models shift colors slightly: sample the palette from the AI
            # output (box-downsampled) so classification matches it.
            rgb1 = np.asarray(Image.fromarray(rgb).resize((w, h), Image.Resampling.BOX))
        if opts.keep_transparency:
            opaque = np.asarray(alpha_img.resize(size, Image.Resampling.LANCZOS)) >= 128
        else:
            opaque = np.ones((size[1], size[0]), bool)
        check()
    else:
        rgb, opaque = rgb1, opaque1

    ctx = _Ctx(opts, scale, rgb1, opaque1, alpha1, rgb, opaque, check)
    layers = _MODE_FUNCS[opts.mode](ctx)
    return TraceResult(w, h, layers, scale, texts, opts.font_family, warnings)
