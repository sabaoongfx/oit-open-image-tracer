#!/usr/bin/env python3
"""Image Tracer - a tiny standalone "Trace Bitmap" app (bitmap -> SVG)."""
from __future__ import annotations

import dataclasses
import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

import cairo  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
THUMB_DIR = os.path.join(HERE, "assets", "thumbs")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                      "image-tracer", "config.json")


def load_config() -> dict:
    try:
        import json
        with open(CONFIG, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def save_config(cfg: dict):
    try:
        import json
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


def thumb_picture(name: str, w: int, h: int) -> Gtk.Widget:
    """Predefined example image for a mode/preset button."""
    path = os.path.join(THUMB_DIR, name + ".png")
    pic = Gtk.Picture.new_for_filename(path) if os.path.exists(path) else Gtk.Picture()
    pic.set_can_shrink(True)
    pic.set_content_fit(Gtk.ContentFit.CONTAIN)
    pic.set_size_request(w, h)
    pic.add_css_class("thumb-pic")
    return pic
import trace_engine as te  # noqa: E402

# (engine id, menu name, hint shown under the menu) - most used first
MODES = [
    ("multi_colors", "Full color",
     "Finds the image's colors and traces each one. For logos, illustrations and screenshots."),
    ("brightness", "Black & white",
     "Dark parts become black shapes. For logos, stamps, scanned text and silhouettes."),
    ("centerline", "Line drawing",
     "Turns each pen line into a single stroke. For sketches, handwriting and signatures."),
    ("edge", "Outlines",
     "Traces only the edges between areas, like a coloring-book page."),
    ("multi_grays", "Grayscale",
     "Like Full color, but in the grays found in the image. For black & white photos and art."),
    ("multi_brightness", "Gray steps",
     "Stacks evenly spaced light-to-dark layers. For shaded artwork."),
    ("quant", "Posterize",
     "Reduces to a few colors, then alternates them black and white. For stencil effects."),
]
# Presets: values applied to the sidebar widgets.
PRESETS = {
    "Logo / illustration": dict(
        mode="multi_colors", scans=8, upscale=0, cleanup=True, merge=1.0, merge_de=8.0,
        edge_smooth=1.0, denoise=0.0, speckles=2, noise_speckles=6, corners=1.0,
        ocr=False, stack=True),
    "Screenshot / UI": dict(
        mode="multi_colors", scans=64, upscale=4, cleanup=False, merge=1.0, merge_de=6.0,
        edge_smooth=0.0, denoise=1.5, speckles=1, noise_speckles=6, corners=1.0,
        ocr=False, stack=True),
    "Line art (centerline)": dict(
        mode="centerline", scans=8, upscale=0, cleanup=True, merge=1.0, merge_de=8.0,
        edge_smooth=1.0, denoise=0.5, speckles=2, noise_speckles=6, corners=1.0,
        ocr=False, stack=True),
    "Screenshot + OCR text": dict(
        mode="multi_colors", scans=64, upscale=4, cleanup=False, merge=1.0, merge_de=6.0,
        edge_smooth=0.0, denoise=1.5, speckles=1, noise_speckles=6, corners=1.0,
        ocr=True, stack=True),
}
PRESET_KEYS = list(PRESETS)
# preset dict key -> TraceOptions field
PRESET_FIELDS = dict(mode="mode", scans="scans", upscale="upscale", cleanup="cleanup",
                     merge="merge_below", merge_de="merge_de", edge_smooth="edge_smooth",
                     denoise="denoise", speckles="speckles", noise_speckles="noise_speckles",
                     corners="smooth_corners", ocr="ocr", stack="stack")

IMAGE_PATTERNS = ["*.png", "*.jpg", "*.jpeg", "*.bmp", "*.gif", "*.tif", "*.tiff", "*.webp"]


class Canvas(Gtk.DrawingArea):
    """Draws either the traced vectors or the original bitmap, fit to view."""

    def __init__(self, thumb: bool = False):
        super().__init__(hexpand=not thumb, vexpand=not thumb)
        self.thumb = thumb
        self.result: te.TraceResult | None = None
        self.surface: cairo.ImageSurface | None = None  # original bitmap
        self.show_original = False
        self.set_draw_func(self._draw)

    def set_original(self, pil_img):
        rgba = pil_img.convert("RGBA")
        w, h = rgba.size
        # cairo ARGB32 is BGRA in memory on little-endian.
        r, g, b, a = rgba.split()
        from PIL import Image

        data = bytearray(Image.merge("RGBA", (b, g, r, a)).tobytes())
        stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, w)
        if stride != w * 4:
            rows = [data[y * w * 4:(y + 1) * w * 4] + bytes(stride - w * 4) for y in range(h)]
            data = bytearray(b"".join(rows))
        self.surface = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_ARGB32, w, h, stride)
        self._data = data  # keep alive
        self.queue_draw()

    def _draw(self, _area, cr, width, height):
        # checkerboard-free plain background
        cr.set_source_rgb(0.85, 0.85, 0.85)
        cr.paint()
        if not self.surface:
            if self.thumb:
                return
            cr.set_source_rgb(0.4, 0.4, 0.4)
            cr.select_font_face("Sans")
            cr.set_font_size(16)
            msg = "Open an image or drop one here"
            ext = cr.text_extents(msg)
            cr.move_to((width - ext.width) / 2, height / 2)
            cr.show_text(msg)
            return
        iw, ih = self.surface.get_width(), self.surface.get_height()
        pad = 3 if self.thumb else 16
        scale = min((width - 2 * pad) / iw, (height - 2 * pad) / ih)
        scale = max(scale, 0.01)
        ox = (width - iw * scale) / 2
        oy = (height - ih * scale) / 2
        cr.translate(ox, oy)
        cr.scale(scale, scale)
        cr.rectangle(0, 0, iw, ih)
        cr.set_source_rgb(1, 1, 1)
        cr.fill()

        if self.show_original or self.result is None:
            cr.set_source_surface(self.surface, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint_with_alpha(0.35 if self.thumb else 1.0)  # thumbs: faded until traced
            return

        cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        for layer in self.result.layers:
            if not layer.contours:
                continue
            for c in layer.contours:
                (sx, sy), segs = c[0], c[1]
                cr.move_to(sx, sy)
                for kind, p in segs:
                    if kind == "C":
                        cr.curve_to(*p)
                    else:
                        cr.line_to(*p)
                if len(c) < 3 or c[2]:
                    cr.close_path()
            r, g, b = layer.color
            cr.set_source_rgb(r / 255, g / 255, b / 255)
            if layer.stroke_width > 0:
                cr.set_line_width(layer.stroke_width)
                cr.set_line_cap(cairo.LINE_CAP_ROUND)
                cr.set_line_join(cairo.LINE_JOIN_ROUND)
                cr.stroke()
            else:
                cr.fill()

        family = self.result.font_family.split(",")[0].strip() or "Sans"
        cr.select_font_face(family, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        for t in self.result.texts:
            cr.set_font_size(t.size)
            adv = cr.text_extents(t.text).x_advance
            if adv <= 0:
                continue
            cr.save()
            cr.translate(t.x, t.y)
            cr.scale(t.width / adv, 1.0)
            r, g, b = t.color
            cr.set_source_rgb(r / 255, g / 255, b / 255)
            cr.move_to(0, 0)
            cr.show_text(t.text)
            cr.restore()


class TracerWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Image Tracer", default_width=1100, default_height=720)
        self.image = None
        self.image_path = None
        self.result = None
        self._debounce_id = 0
        self._generation = 0
        self._applying_preset = False
        self.current_preset = 0  # index into PRESET_KEYS, None = custom

        # ---- header bar ----
        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        open_btn = Gtk.Button(label="Open…")
        open_btn.connect("clicked", self.on_open)
        header.pack_start(open_btn)

        self.config = load_config()
        lvl = Gtk.Box(css_classes=["linked"])
        self.simple_btn = Gtk.ToggleButton(label="Simple")
        self.adv_btn = Gtk.ToggleButton(label="Advanced", group=self.simple_btn)
        lvl.append(self.simple_btn)
        lvl.append(self.adv_btn)
        header.pack_start(lvl)

        self.export_btn = Gtk.Button(label="Export SVG…", sensitive=False)
        self.export_btn.add_css_class("suggested-action")
        self.export_btn.connect("clicked", self.on_export)
        header.pack_end(self.export_btn)

        self.copy_btn = Gtk.Button(label="Copy SVG", sensitive=False)
        self.copy_btn.connect("clicked", self.on_copy)
        header.pack_end(self.copy_btn)

        view_box = Gtk.Box(css_classes=["linked"])
        self.vec_toggle = Gtk.ToggleButton(label="Vector", active=True)
        self.orig_toggle = Gtk.ToggleButton(label="Original", group=self.vec_toggle)
        self.vec_toggle.connect("toggled", self.on_view_toggled)
        view_box.append(self.vec_toggle)
        view_box.append(self.orig_toggle)
        header.set_title_widget(view_box)

        # ---- layout ----
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=350,
                          shrink_start_child=False, resize_start_child=False)
        self.set_child(paned)

        side_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=14, margin_bottom=14, margin_start=14, margin_end=14)
        side_scroll.set_child(side)
        paned.set_start_child(side_scroll)

        canvas_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.canvas = Canvas()
        canvas_box.append(self.canvas)
        self.status = Gtk.Label(xalign=0, margin_start=10, margin_end=10, margin_top=4, margin_bottom=4,
                                label="No image loaded", css_classes=["dim-label"])
        canvas_box.append(self.status)
        paned.set_end_child(canvas_box)

        # drag & drop
        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_drop)
        self.canvas.add_controller(drop)

        # ---- controls ----
        side.append(self._heading("Preset"))
        flow = Gtk.FlowBox(max_children_per_line=3, min_children_per_line=3, homogeneous=True,
                           selection_mode=Gtk.SelectionMode.NONE, column_spacing=6,
                           row_spacing=6)
        self.thumbs = []
        for i, name in enumerate(PRESET_KEYS):
            canvas = thumb_picture(f"preset_{i}", 88, 60)
            label = Gtk.Label(label=name, wrap=True, justify=Gtk.Justification.CENTER,
                              css_classes=["thumb-label"])
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.append(canvas)
            box.append(label)
            btn = Gtk.ToggleButton(child=box, tooltip_text=name, css_classes=["preset-thumb"])
            btn.connect("toggled", self.on_thumb_toggled, i)
            flow.append(btn)
            self.thumbs.append((btn, canvas, label))
        side.append(flow)
        self.custom_lbl = Gtk.Label(label="Custom settings", xalign=0, css_classes=["dim-label"],
                                    visible=False)
        side.append(self.custom_lbl)


        side.append(Gtk.Separator(margin_top=6, margin_bottom=2))
        side.append(self._heading("Mode"))
        mflow = Gtk.FlowBox(max_children_per_line=3, min_children_per_line=3, homogeneous=True,
                            selection_mode=Gtk.SelectionMode.NONE, column_spacing=4,
                            row_spacing=4)
        self.mode_btns = []
        for i, (mid, name, hint) in enumerate(MODES):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.append(thumb_picture(f"mode_{mid}", 88, 60))
            box.append(Gtk.Label(label=name, ellipsize=3, css_classes=["thumb-label"]))
            b = Gtk.ToggleButton(child=box, tooltip_text=hint, css_classes=["mode-btn"])
            b.connect("toggled", self.on_mode_btn, i)
            mflow.append(b)
            self.mode_btns.append(b)
        side.append(mflow)
        self.mode_hint = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        side.append(self.mode_hint)
        self._mode_idx = 0

        # Everything below is only shown in Advanced.
        self.adv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        side.append(self.adv)
        main_side, side = side, self.adv
        side.append(Gtk.Separator(margin_top=6, margin_bottom=2))
        side.append(self._heading("Settings"))

        self.threshold = self._scale(side, "Threshold", 0.0, 1.0, 0.01, 0.45, digits=2)
        self.quant_colors = self._spin(side, "Colors", 2, 32, 8)
        self.scans = self._spin(side, "Max colors", 2, 128, 8)

        self.stroke_w = self._scale(side, "Stroke width (0 = auto)", 0.0, 30.0, 0.25, 0.0, digits=2)
        self.invert = self._check(side, "Invert image")
        self.smooth = self._check(side, "Smooth (blur before scanning)")
        self.stack = self._check(side, "Stack scans", True)
        self.remove_bg = self._check(side, "Remove background")
        self.merge_de = self._scale(side, "Merge similar colors (ΔE)", 1.0, 25.0, 0.5, 8.0, digits=1)
        self.palette_entry = Gtk.Entry(placeholder_text="#1b1b1b, #e0c088, #5a7d3c")
        self.palette_entry.connect("changed", self.schedule)
        self.palette_entry._row = self._row(side, "Fixed palette (optional, hex colors)",
                                            self.palette_entry)
        self.cleanup = self._check(side, "Clean up anti-aliasing colors (logos)", True)
        self.merge = self._scale(side, "Clean up colors under (% of image)", 0.0, 10.0, 0.1, 1.0, digits=1)
        self.cleanup.connect("toggled", self.on_mode_changed)

        side.append(Gtk.Separator(margin_top=6, margin_bottom=2))
        side.append(self._heading("Quality"))
        self.upscale_dd = Gtk.DropDown.new_from_strings(["Auto", "1× (none)", "2×", "3×", "4×"])
        self.upscale_dd.connect("notify::selected", self.schedule)
        self._row(side, "Upscale before tracing", self.upscale_dd)
        self.upscalers = ["lanczos"] + te.ai_upscalers()
        self.upscaler_dd = Gtk.DropDown.new_from_strings(
            ["Lanczos (fast)"] + [f"{n} (AI, GPU)" for n in self.upscalers[1:]])
        self.upscaler_dd.connect("notify::selected", self.schedule)
        self._row(side, "Upscaler", self.upscaler_dd)
        if len(self.upscalers) == 1:
            side.append(Gtk.Label(
                label="For AI upscaling install one of: waifu2x-ncnn-vulkan (pacman), "
                      "realesrgan-ncnn-vulkan or realcugan-ncnn-vulkan (AUR), then restart.",
                xalign=0, wrap=True, css_classes=["dim-label"], selectable=True))
        self.keep_alpha = self._check(side, "Keep transparency", True)
        self.edge_smooth = self._scale(side, "Edge smoothing", 0.0, 3.0, 0.05, 1.0, digits=2)
        self.denoise = self._scale(side, "Denoise (JPEG noise)", 0.0, 3.0, 0.1, 0.0, digits=1)

        side.append(Gtk.Separator(margin_top=6, margin_bottom=2))
        side.append(self._heading("Text (OCR)"))
        self.ocr = self._check(side, "Recognize text → editable SVG text")
        self.ocr_conf = self._scale(side, "Min. OCR confidence", 30, 95, 1, 70, digits=0)
        self.font_entry = Gtk.Entry(text=te.DEFAULT_FONT)
        self.font_entry.connect("changed", self.schedule)
        self.font_entry._row = self._row(side, "Font family", self.font_entry)
        ok, msg = te.ocr_status()
        self.ocr_msg = Gtk.Label(label=msg, xalign=0, wrap=True, css_classes=["dim-label"],
                                 selectable=True, visible=not ok)
        side.append(self.ocr_msg)
        self.ocr.connect("toggled", self.on_mode_changed)

        side.append(Gtk.Separator(margin_top=6, margin_bottom=2))
        side.append(self._heading("Options"))
        self.speckles = self._spin(side, "Suppress speckles (px)", 0, 50, 2)
        self.noise_speckles = self._spin(side, "Suppress low-contrast speckles (px)", 0, 50, 6)
        self.corners = self._scale(side, "Smooth corners", 0.0, 1.34, 0.01, 1.0, digits=2)
        self.optimize_on = self._check(side, "Optimize paths", True)
        self.optimize = self._scale(side, "Optimize tolerance", 0.0, 5.0, 0.05, 0.2, digits=2)

        side.append(Gtk.Separator(margin_top=6, margin_bottom=2))
        self.auto = self._check(side, "Live preview", True, retrace=False)
        self.update_btn = Gtk.Button(label="Update preview")
        self.update_btn.connect("clicked", lambda *_: self.retrace())
        side.append(self.update_btn)
        side = main_side
        self.spinner = Gtk.Spinner()
        side.append(self.spinner)

        self.adv_btn.set_active(bool(self.config.get("advanced", False)))
        self.simple_btn.set_active(not self.adv_btn.get_active())
        self.adv.set_visible(self.adv_btn.get_active())
        self.adv_btn.connect("toggled", self.on_level_toggled)

        self._mark_preset(0)
        self.on_preset()

    # ---- presets ----
    def _mark_preset(self, idx):
        """Highlight thumbnail idx (None = custom) without side effects."""
        self.current_preset = idx
        self._applying_preset = True
        try:
            for i, (btn, _c, _l) in enumerate(self.thumbs):
                btn.set_active(i == idx)
        finally:
            self._applying_preset = False
        self.custom_lbl.set_visible(idx is None)

    def on_thumb_toggled(self, btn, idx):
        if self._applying_preset:
            return
        self._mark_preset(idx)  # keeps exactly one active (re-activates on second click)
        self.on_preset()

    def _preset_options(self, p) -> te.TraceOptions:
        """Current settings with the preset's values applied."""
        return dataclasses.replace(self.options(),
                                   **{PRESET_FIELDS[k]: v for k, v in p.items()})

    def on_preset(self, *_):
        if self.current_preset is None:
            return
        idx = self.current_preset
        p = PRESETS[PRESET_KEYS[idx]]
        self._applying_preset = True
        try:
            self._set_mode([m[0] for m in MODES].index(p["mode"]))
            self.scans.set_value(p["scans"])
            self.upscale_dd.set_selected(p["upscale"])
            self.cleanup.set_active(p["cleanup"])
            self.merge.set_value(p["merge"])
            self.merge_de.set_value(p["merge_de"])
            self.edge_smooth.set_value(p["edge_smooth"])
            self.denoise.set_value(p["denoise"])
            self.speckles.set_value(p["speckles"])
            self.noise_speckles.set_value(p["noise_speckles"])
            self.corners.set_value(p["corners"])
            self.ocr.set_active(p["ocr"])
            self.stack.set_active(p["stack"])
            self.on_mode_changed()
        finally:
            self._applying_preset = False
        self.schedule()

    # ---- simple / advanced ----
    def on_level_toggled(self, btn):
        adv = btn.get_active()
        self.adv.set_visible(adv)
        self.config["advanced"] = adv
        save_config(self.config)

    # ---- widget helpers ----
    def _heading(self, text):
        lbl = Gtk.Label(label=text, xalign=0)
        lbl.add_css_class("heading")
        return lbl

    def _row(self, parent, text, widget):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(Gtk.Label(label=text, xalign=0))
        box.append(widget)
        parent.append(box)
        return box

    def _slider_with_entry(self, s, digits):
        """Slider + a small text field to type the value; kept in sync."""
        s.set_draw_value(False)
        s.set_hexpand(True)
        e = Gtk.Entry(width_chars=6, max_width_chars=6, xalign=1.0,
                      input_purpose=Gtk.InputPurpose.NUMBER, css_classes=["value-entry"])
        fmt = f"{{:.{digits}f}}"

        def show(*_):
            e.set_text(fmt.format(s.get_value()))

        def commit(*_):
            adj = s.get_adjustment()
            try:
                v = float(e.get_text().strip().replace(",", "."))
            except ValueError:
                show()
                return
            v = min(max(v, adj.get_lower()), adj.get_upper())
            v = round(v, digits)
            if abs(v - s.get_value()) > 1e-9:
                s.set_value(v)
            show()

        s.connect("value-changed", show)
        e.connect("activate", commit)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", commit)
        e.add_controller(focus)
        show()
        box = Gtk.Box(spacing=6)
        box.append(s)
        box.append(e)
        s._entry = e
        return box

    def _scale(self, parent, text, lo, hi, step, val, digits=2):
        s = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        s.set_value(val)
        s.set_digits(digits)
        s.connect("value-changed", self.schedule)
        s._row = self._row(parent, text, self._slider_with_entry(s, digits))
        return s

    def _spin(self, parent, text, lo, hi, val):
        """Whole-number slider (used where a +/- number box was)."""
        s = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, 1)
        s.set_digits(0)
        s.set_round_digits(0)
        s.set_value(val)
        s.get_value_as_int = lambda: int(round(s.get_value()))
        s.connect("value-changed", self.schedule)
        s._row = self._row(parent, text, self._slider_with_entry(s, 0))
        return s

    def _check(self, parent, text, active=False, retrace=True):
        c = Gtk.CheckButton(label=text, active=active)
        if retrace:
            c.connect("toggled", self.schedule)
        c._row = c
        parent.append(c)
        return c

    # ---- options ----
    def mode(self):
        return MODES[self._mode_idx][0]

    def _set_mode(self, idx):
        """Select mode idx and update the buttons without side effects."""
        self._mode_idx = idx
        guard = self._applying_preset
        self._applying_preset = True
        try:
            for i, b in enumerate(self.mode_btns):
                b.set_active(i == idx)
        finally:
            self._applying_preset = guard

    def on_mode_btn(self, btn, idx):
        if self._applying_preset:
            return
        self._set_mode(idx)  # keeps exactly one active
        self._mark_preset(None)  # a hand-picked mode means custom settings
        self.on_mode_changed()

    def options(self) -> te.TraceOptions:
        return te.TraceOptions(
            mode=self.mode(),
            threshold=self.threshold.get_value(),
            quant_colors=self.quant_colors.get_value_as_int(),
            scans=self.scans.get_value_as_int(),
            invert=self.invert.get_active(),
            smooth=self.smooth.get_active(),
            stack=self.stack.get_active(),
            remove_background=self.remove_bg.get_active(),
            speckles=self.speckles.get_value_as_int(),
            smooth_corners=self.corners.get_value(),
            optimize=self.optimize.get_value(),
            optimize_enabled=self.optimize_on.get_active(),
            upscale=self.upscale_dd.get_selected(),  # 0=auto, 1..4
            merge_below=self.merge.get_value(),
            keep_transparency=self.keep_alpha.get_active(),
            edge_smooth=self.edge_smooth.get_value(),
            denoise=self.denoise.get_value(),
            cleanup=self.cleanup.get_active(),
            merge_de=self.merge_de.get_value(),
            noise_speckles=self.noise_speckles.get_value_as_int(),
            ocr=self.ocr.get_active(),
            ocr_min_conf=self.ocr_conf.get_value(),
            font_family=self.font_entry.get_text() or te.DEFAULT_FONT,
            palette=self.palette_entry.get_text(),
            upscaler=self.upscalers[self.upscaler_dd.get_selected()],
            stroke_width=self.stroke_w.get_value(),
        )

    def on_mode_changed(self, *_):
        m = self.mode()
        single = m in ("brightness", "edge", "quant", "centerline")
        self.mode_hint.set_label(MODES[self._mode_idx][2])
        self.threshold._row.set_visible(m in ("brightness", "edge", "centerline"))
        self.stroke_w._row.set_visible(m == "centerline")
        self.quant_colors._row.set_visible(m == "quant")
        self.scans._row.set_visible(not single)
        self.invert.set_visible(single)
        self.smooth.set_visible(m in ("multi_colors", "multi_grays"))
        self.stack.set_visible(not single)
        self.remove_bg.set_visible(not single)
        colors = m in ("quant", "multi_colors", "multi_grays")
        self.merge_de._row.set_visible(colors)
        self.palette_entry._row.set_visible(colors)
        self.cleanup.set_visible(colors)
        self.merge._row.set_visible(colors and self.cleanup.get_active())
        self.edge_smooth._row.set_visible(colors)
        self.noise_speckles._row.set_visible(m in ("multi_colors", "multi_grays"))
        on = self.ocr.get_active()
        self.ocr_conf._row.set_visible(on)
        self.font_entry._row.set_visible(on)
        self.schedule()

    # ---- tracing ----
    def schedule(self, *_):
        if self._applying_preset:
            return
        if _ and _[0] is not self.auto and getattr(self, "current_preset", None) is not None \
                and hasattr(self, "thumbs"):
            self._mark_preset(None)
        if not self.image or not self.auto.get_active():
            return
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(250, self._debounced)

    def _debounced(self):
        self._debounce_id = 0
        self.retrace()
        return False

    def retrace(self):
        if not self.image:
            return
        self._generation += 1
        gen = self._generation
        opts = self.options()
        img = self.image
        self.spinner.start()
        self.status.set_label("Tracing…")

        def work():
            try:
                res = te.trace(img, opts, cancelled=lambda: gen != self._generation)
                err = None
            except Exception as e:  # noqa: BLE001
                res, err = None, e
            GLib.idle_add(self._done, gen, res, err)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, gen, res, err):
        if gen != self._generation:
            return False
        self.spinner.stop()
        if err:
            self.status.set_label(f"Error: {err}")
            return False
        self.result = res
        self.canvas.result = res
        self.canvas.queue_draw()

        has = res is not None and res.path_count > 0
        self.export_btn.set_sensitive(has)
        self.copy_btn.set_sensitive(has)
        self.status.set_label(
            f"{os.path.basename(self.image_path)} — {res.width}×{res.height}px (traced at {res.scale}×) — "
            f"{len(res.layers)} colors, {res.path_count} paths, {res.node_count} nodes"
            + (f", {len(res.texts)} text lines" if res.texts else "")
            + ("" if not self.ocr.get_active() or te.ocr_available()
               else " — OCR unavailable (see sidebar)")
        )
        return False

    # ---- file handling ----
    def load(self, path):
        try:
            img = te.load_image(path)
        except Exception as e:  # noqa: BLE001
            self.status.set_label(f"Could not open {path}: {e}")
            return
        self.image, self.image_path = img, path
        self.result = None
        self.canvas.result = None
        self.canvas.set_original(img)
        self.set_title(f"Image Tracer — {os.path.basename(path)}")
        self.retrace()

    def on_drop(self, _target, value, _x, _y):
        if value and value.get_path():
            self.load(value.get_path())
            return True
        return False

    def on_open(self, *_):
        dlg = Gtk.FileDialog(title="Open image")
        f = Gtk.FileFilter(name="Images")
        for p in IMAGE_PATTERNS:
            f.add_pattern(p)
            f.add_pattern(p.upper())
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dlg.set_filters(filters)
        dlg.set_default_filter(f)

        def done(d, r):
            try:
                file = d.open_finish(r)
            except GLib.Error:
                return
            if file:
                self.load(file.get_path())

        dlg.open(self, None, done)

    def on_export(self, *_):
        if not self.result:
            return
        dlg = Gtk.FileDialog(title="Export SVG")
        base = os.path.splitext(os.path.basename(self.image_path))[0] + ".svg"
        dlg.set_initial_name(base)
        if self.image_path:
            dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self.image_path)))
        svg = self.result.to_svg()

        def done(d, r):
            try:
                file = d.save_finish(r)
            except GLib.Error:
                return
            if file:
                with open(file.get_path(), "w", encoding="utf-8") as fh:
                    fh.write(svg)
                self.status.set_label(f"Saved {file.get_path()}")

        dlg.save(self, None, done)

    def on_copy(self, *_):
        if self.result:
            self.get_clipboard().set(self.result.to_svg())
            self.status.set_label("SVG copied to clipboard")

    def on_view_toggled(self, *_):
        self.canvas.show_original = not self.vec_toggle.get_active()
        self.canvas.queue_draw()


CSS = b"""
.preset-thumb { padding: 3px; border: 2px solid transparent; }
.thumb-label { font-size: 0.8em; }
.thumb-pic { border-radius: 4px; }
.value-entry { min-height: 0; padding-top: 2px; padding-bottom: 2px; }
.mode-btn { padding: 3px; border: 2px solid transparent; }
.mode-btn:checked { border-color: @theme_selected_bg_color; background: alpha(@theme_selected_bg_color, 0.18); }
.preset-thumb:checked { border-color: @theme_selected_bg_color; background: alpha(@theme_selected_bg_color, 0.18); }
"""


class TracerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="local.imagetracer",
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.props.active_window or TracerWindow(self)
        win.present()

    def do_open(self, files, _n, _hint):
        win = self.props.active_window or TracerWindow(self)
        win.present()
        if files:
            win.load(files[0].get_path())


if __name__ == "__main__":
    sys.exit(TracerApp().run(sys.argv))
