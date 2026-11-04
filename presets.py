"""Modes and presets: what the app offers, independent of the GUI toolkit.

Used by the GUI (image_tracer.py) and by tools/make_thumbs.py. Every key in a
preset's ``options`` and every name in a mode's ``settings`` is a
``trace_engine.TraceOptions`` field (checked by tests/test_engine.py).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModeSpec:
    id: str  # trace_engine mode id
    name: str
    hint: str
    settings: frozenset[str]  # mode-dependent settings this mode uses


@dataclass(frozen=True)
class PresetSpec:
    slug: str  # used for the thumbnail file name
    name: str
    options: dict  # TraceOptions field -> value
    sample: str  # sample image (assets/samples) shown on its thumbnail


_COLOR = frozenset({"merge_de", "palette", "cleanup", "merge_below", "edge_smooth"})
_LAYERED = frozenset({"stack", "remove_background"})
_BW = frozenset({"threshold", "invert"})
_FULL_COLOR = _COLOR | _LAYERED | {"scans", "smooth", "noise_speckles"}

# Most used first.
MODES: tuple[ModeSpec, ...] = (
    ModeSpec("multi_colors", "Full color",
             "Finds the image's colors and traces each one. "
             "For logos, illustrations and screenshots.", _FULL_COLOR),
    ModeSpec("brightness", "Black & white",
             "Dark parts become black shapes. For logos, stamps, scanned text and silhouettes.",
             _BW),
    ModeSpec("centerline", "Line drawing",
             "Turns each pen line into a single stroke. For sketches, handwriting and signatures.",
             _BW | {"stroke_width"}),
    ModeSpec("edge", "Outlines",
             "Traces only the edges between areas, like a coloring-book page.", _BW),
    ModeSpec("multi_grays", "Grayscale",
             "Like Full color, but in the grays found in the image. "
             "For black & white photos and art.", _FULL_COLOR),
    ModeSpec("multi_brightness", "Gray steps",
             "Stacks evenly spaced light-to-dark layers. For shaded artwork.",
             _LAYERED | {"scans"}),
    ModeSpec("quant", "Posterize",
             "Reduces to a few colors, then alternates them black and white. "
             "For stencil effects.", _COLOR | {"quant_colors", "invert"}),
)

# Settings that only some modes use; every other setting is always shown.
MODE_DEPENDENT: frozenset[str] = frozenset().union(*(m.settings for m in MODES))

# Settings shown only while another (on/off) setting is on.
REQUIRES = {"merge_below": "cleanup", "ocr_min_conf": "ocr", "font_family": "ocr"}

_LOGO = dict(mode="multi_colors", scans=8, upscale=0, cleanup=True, merge_below=1.0,
             merge_de=8.0, edge_smooth=1.0, denoise=0.0, speckles=2, noise_speckles=6,
             smooth_corners=1.0, ocr=False, stack=True)
_SCREENSHOT = {**_LOGO, "scans": 64, "upscale": 4, "cleanup": False, "merge_de": 6.0,
               "edge_smooth": 0.0, "denoise": 1.5, "speckles": 1}

PRESETS: tuple[PresetSpec, ...] = (
    PresetSpec("logo", "Logo / illustration", _LOGO, "logo"),
    PresetSpec("screenshot", "Screenshot / UI", _SCREENSHOT, "ui"),
    PresetSpec("line-art", "Line art (centerline)",
               {**_LOGO, "mode": "centerline", "denoise": 0.5}, "sketch"),
    PresetSpec("screenshot-ocr", "Screenshot + OCR text", {**_SCREENSHOT, "ocr": True}, "ui"),
)


def mode_index(mode_id: str) -> int:
    return next(i for i, m in enumerate(MODES) if m.id == mode_id)
