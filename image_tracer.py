#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Image Tracer - a small standalone "Trace Bitmap" app (bitmap -> SVG)."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

import cairo  # noqa: E402

import presets as P  # noqa: E402
import trace_engine as te  # noqa: E402

log = logging.getLogger("image_tracer")

APP_ID = "io.github.sabaoongfx.OpenImageTracer"  # icon, .desktop and Flatpak id
# Source folder, or the unpacked bundle of a frozen (PyInstaller) build.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
THUMB_DIR = os.path.join(HERE, "assets", "thumbs")
ICON_DIR = os.path.join(HERE, "assets", "icons")  # hicolor theme with APP_ID icons
# Per-OS config folder (~/.config on Linux, %APPDATA% on Windows, ~/Library/... on macOS).
CONFIG = os.path.join(GLib.get_user_config_dir(), "image-tracer", "config.json")
IMAGE_PATTERNS = ["*.png", "*.jpg", "*.jpeg", "*.bmp", "*.gif", "*.tif", "*.tiff", "*.webp"]
THUMB_W, THUMB_H = 88, 60
DEBOUNCE_MS = 250

CSS = b"""
.thumb-btn { padding: 3px; border: 2px solid transparent; }
.thumb-btn:checked { border-color: @theme_selected_bg_color;
                     background: alpha(@theme_selected_bg_color, 0.18); }
.thumb-label { font-size: 0.8em; }
.thumb-pic { border-radius: 4px; }
.value-entry { min-height: 0; padding-top: 2px; padding-bottom: 2px; }
.vector-pane { border-left: 1px solid alpha(currentColor, 0.15); }
"""


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def load_config() -> dict:
    try:
        with open(CONFIG, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable config %s: %s", CONFIG, e)
        return {}


def save_config(cfg: dict):
    try:
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError as e:
        log.warning("cannot save config %s: %s", CONFIG, e)


# --------------------------------------------------------------------------
# Settings: one labelled control bound to one TraceOptions field
# --------------------------------------------------------------------------
def _labelled(text: str, widget: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.append(Gtk.Label(label=text, xalign=0))
    box.append(widget)
    return box


class Setting:
    """Base class. ``row`` is the widget shown/hidden as a whole; ``on_change``
    is called with the field name whenever the value changes."""

    def __init__(self, name: str, row: Gtk.Widget, on_change):
        self.name = name
        self.row = row
        self._on_change = on_change

    def _changed(self, *_):
        self._on_change(self.name)

    def get(self):
        raise NotImplementedError

    def set(self, value):
        raise NotImplementedError


class SliderSetting(Setting):
    """Slider plus a small text field to type the value (kept in sync)."""

    def __init__(self, name, label, lo, hi, step, value, on_change, digits=2):
        self.digits = digits
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        self.scale.set_digits(digits)
        if digits == 0:
            self.scale.set_round_digits(0)
        self.scale.set_draw_value(False)
        self.scale.set_hexpand(True)
        self.scale.set_value(value)
        self.entry = Gtk.Entry(width_chars=6, max_width_chars=6, xalign=1.0,
                               input_purpose=Gtk.InputPurpose.NUMBER, css_classes=["value-entry"])
        self.entry.connect("activate", self._commit)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", self._commit)
        self.entry.add_controller(focus)
        self._show()
        self.scale.connect("value-changed", self._scale_moved)
        box = Gtk.Box(spacing=6)
        box.append(self.scale)
        box.append(self.entry)
        super().__init__(name, _labelled(label, box), on_change)

    def _show(self):
        self.entry.set_text(f"{self.scale.get_value():.{self.digits}f}")

    def _scale_moved(self, *_):
        self._show()
        self._changed()

    def _commit(self, *_):
        adj = self.scale.get_adjustment()
        try:
            v = float(self.entry.get_text().strip().replace(",", "."))
        except ValueError:
            self._show()
            return
        v = round(min(max(v, adj.get_lower()), adj.get_upper()), self.digits)
        if abs(v - self.scale.get_value()) > 1e-9:
            self.scale.set_value(v)  # emits value-changed -> _scale_moved
        self._show()

    def get(self):
        v = self.scale.get_value()
        return int(round(v)) if self.digits == 0 else v

    def set(self, value):
        self.scale.set_value(value)


class CheckSetting(Setting):
    def __init__(self, name, label, value, on_change):
        self.check = Gtk.CheckButton(label=label, active=value)
        self.check.connect("toggled", self._changed)
        super().__init__(name, self.check, on_change)

    def get(self):
        return self.check.get_active()

    def set(self, value):
        self.check.set_active(bool(value))


class EntrySetting(Setting):
    def __init__(self, name, label, value, on_change, placeholder="", empty_value=""):
        self.empty_value = empty_value
        self.entry = Gtk.Entry(text=value, placeholder_text=placeholder)
        self.entry.connect("changed", self._changed)
        super().__init__(name, _labelled(label, self.entry), on_change)

    def get(self):
        return self.entry.get_text() or self.empty_value

    def set(self, value):
        self.entry.set_text(value)


class ChoiceSetting(Setting):
    def __init__(self, name, label, choices: list[tuple[str, object]], on_change):
        self.values = [v for _, v in choices]
        self.dropdown = Gtk.DropDown.new_from_strings([t for t, _ in choices])
        self.dropdown.connect("notify::selected", self._changed)
        super().__init__(name, _labelled(label, self.dropdown), on_change)

    def get(self):
        return self.values[self.dropdown.get_selected()]

    def set(self, value):
        self.dropdown.set_selected(self.values.index(value))


# --------------------------------------------------------------------------
# Background tracing
# --------------------------------------------------------------------------
class TraceWorker:
    """Runs one trace at a time on a background thread. A new request makes
    the running trace stop at its next checkpoint and replaces any queued one,
    so dragging a slider never piles up work."""

    def __init__(self, on_done):
        self._on_done = on_done  # (generation, result, error) on the GTK main loop
        self._cond = threading.Condition()
        self._pending = None
        self.generation = 0
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, img, opts) -> int:
        with self._cond:
            self.generation += 1
            self._pending = (self.generation, img, opts)
            self._cond.notify()
            return self.generation

    def _run(self):
        while True:
            with self._cond:
                while self._pending is None:
                    self._cond.wait()
                gen, img, opts = self._pending
                self._pending = None
            try:
                res, err = te.trace(img, opts, cancelled=lambda: gen != self.generation), None
            except te.Cancelled:
                continue
            except Exception as e:  # noqa: BLE001 - report any engine failure in the UI
                log.exception("trace failed")
                res, err = None, e
            GLib.idle_add(self._on_done, gen, res, err)


# --------------------------------------------------------------------------
# Canvas
# --------------------------------------------------------------------------
class Canvas(Gtk.DrawingArea):
    """Draws the original bitmap or the traced vectors, fit to view."""
    PAD = 16

    def __init__(self, show_original: bool = False):
        super().__init__(hexpand=True, vexpand=True)
        self.result: te.TraceResult | None = None
        self.surface: cairo.ImageSurface | None = None  # original bitmap
        self.show_original = show_original
        self.set_draw_func(self._draw)

    def set_original(self, pil_img):
        rgba = pil_img.convert("RGBA")
        w, h = rgba.size
        r, g, b, a = rgba.split()
        from PIL import Image  # local: only needed here
        data = bytearray(Image.merge("RGBA", (b, g, r, a)).tobytes())  # cairo wants BGRA
        stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, w)
        if stride != w * 4:
            data = bytearray(b"".join(data[y * w * 4:(y + 1) * w * 4] + bytes(stride - w * 4)
                                      for y in range(h)))
        self.surface = cairo.ImageSurface.create_for_data(data, cairo.FORMAT_ARGB32, w, h, stride)
        self._data = data  # keep the buffer alive
        self.queue_draw()

    def _draw(self, _area, cr, width, height):
        cr.set_source_rgb(0.85, 0.85, 0.85)
        cr.paint()
        if not self.surface:
            cr.set_source_rgb(0.4, 0.4, 0.4)
            cr.select_font_face("Sans")
            cr.set_font_size(16)
            msg = "Open an image or drop one here"
            ext = cr.text_extents(msg)
            cr.move_to((width - ext.width) / 2, height / 2)
            cr.show_text(msg)
            return
        iw, ih = self.surface.get_width(), self.surface.get_height()
        scale = max(min((width - 2 * self.PAD) / iw, (height - 2 * self.PAD) / ih), 0.01)
        cr.translate((width - iw * scale) / 2, (height - ih * scale) / 2)
        cr.scale(scale, scale)
        cr.rectangle(0, 0, iw, ih)
        cr.set_source_rgb(1, 1, 1)
        cr.fill()
        if self.show_original:
            cr.set_source_surface(self.surface, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
        elif self.result is not None:
            self._draw_result(cr, self.result)
        else:  # first trace still running: faded image + note, never a fake "result"
            cr.set_source_surface(self.surface, 0, 0)
            cr.paint_with_alpha(0.2)
            cr.identity_matrix()
            cr.set_source_rgb(0.25, 0.25, 0.25)
            cr.select_font_face("Sans")
            cr.set_font_size(15)
            ext = cr.text_extents("Tracing…")
            cr.move_to((width - ext.width) / 2, height / 2)
            cr.show_text("Tracing…")

    @staticmethod
    def _draw_result(cr, result: te.TraceResult):
        cr.set_fill_rule(cairo.FILL_RULE_WINDING)
        for layer in result.layers:
            if not layer.contours:
                continue
            for c in layer.contours:
                cr.move_to(*c.start)
                for kind, p in c.segs:
                    if kind == "C":
                        cr.curve_to(*p)
                    else:
                        cr.line_to(*p)
                if c.closed:
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
        family = result.font_family.split(",")[0].strip() or "Sans"
        cr.select_font_face(family, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        for t in result.texts:
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


def thumb_button(image_name: str, label: str, tooltip: str, wrap: bool) -> Gtk.ToggleButton:
    """Toggle button with a predefined example image (assets/thumbs) and a label."""
    path = os.path.join(THUMB_DIR, image_name + ".png")
    pic = Gtk.Picture.new_for_filename(path) if os.path.exists(path) else Gtk.Picture()
    pic.set_can_shrink(True)
    pic.set_content_fit(Gtk.ContentFit.CONTAIN)
    pic.set_size_request(THUMB_W, THUMB_H)
    pic.add_css_class("thumb-pic")
    text = Gtk.Label(label=label, css_classes=["thumb-label"])
    if wrap:
        text.set_wrap(True)
        text.set_justify(Gtk.Justification.CENTER)
    else:
        text.set_ellipsize(Pango.EllipsizeMode.END)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.append(pic)
    box.append(text)
    return Gtk.ToggleButton(child=box, tooltip_text=tooltip, css_classes=["thumb-btn"])


def _grid() -> Gtk.FlowBox:
    return Gtk.FlowBox(max_children_per_line=3, min_children_per_line=3, homogeneous=True,
                       selection_mode=Gtk.SelectionMode.NONE, column_spacing=4, row_spacing=4)


def _titled(title: str, child: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.append(Gtk.Label(label=title, xalign=0, margin_start=10, margin_top=4,
                         margin_bottom=2, css_classes=["caption-heading", "dim-label"]))
    box.append(child)
    return box


def _heading(text: str) -> Gtk.Label:
    return Gtk.Label(label=text, xalign=0, css_classes=["heading"])


def _separator() -> Gtk.Separator:
    return Gtk.Separator(margin_top=6, margin_bottom=2)


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
class TracerWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Image Tracer",
                         default_width=1100, default_height=720)
        self.image = None
        self.image_path = None
        self.result: te.TraceResult | None = None
        self.config = load_config()
        self.settings: dict[str, Setting] = {}
        self.mode_idx = 0
        self.preset_idx: int | None = 0  # None = custom settings
        self._quiet_depth = 0
        self._debounce_id = 0
        self.worker = TraceWorker(self._trace_done)

        self._build_header()
        sidebar = self._build_layout()
        self._build_presets(sidebar)
        self._build_modes(sidebar)
        self.adv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        sidebar.append(self.adv_box)
        self._build_advanced(self.adv_box)
        self.spinner = Gtk.Spinner()
        sidebar.append(self.spinner)

        advanced = bool(self.config.get("advanced", False))
        self.adv_btn.set_active(advanced)
        self.simple_btn.set_active(not advanced)
        self.adv_box.set_visible(advanced)
        self.adv_btn.connect("toggled", self._on_level_toggled)
        self.apply_preset(0)

    # ---- construction ----
    def _build_header(self):
        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        open_btn = Gtk.Button(label="Open…")
        open_btn.connect("clicked", self.on_open)
        header.pack_start(open_btn)

        level = Gtk.Box(css_classes=["linked"])
        self.simple_btn = Gtk.ToggleButton(label="Simple")
        self.adv_btn = Gtk.ToggleButton(label="Advanced", group=self.simple_btn)
        level.append(self.simple_btn)
        level.append(self.adv_btn)
        header.pack_start(level)

        self.export_btn = Gtk.Button(label="Export SVG…", sensitive=False,
                                     css_classes=["suggested-action"])
        self.export_btn.connect("clicked", self.on_export)
        header.pack_end(self.export_btn)
        self.copy_btn = Gtk.Button(label="Copy SVG", sensitive=False)
        self.copy_btn.connect("clicked", self.on_copy)
        header.pack_end(self.copy_btn)
        header.set_title_widget(Gtk.Label(label="Image Tracer", css_classes=["title"]))

    def _build_layout(self) -> Gtk.Box:
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=350,
                          shrink_start_child=False, resize_start_child=False)
        self.set_child(paned)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=14,
                          margin_bottom=14, margin_start=14, margin_end=14)
        scroll.set_child(sidebar)
        paned.set_start_child(scroll)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.orig_canvas = Canvas(show_original=True)
        self.canvas = Canvas()  # the vector result
        # Equal halves, so both previews are drawn at the same size.
        previews = Gtk.Box(homogeneous=True, vexpand=True)
        previews.append(_titled("Image", self.orig_canvas))
        vector = _titled("Vector", self.canvas)
        vector.add_css_class("vector-pane")
        previews.append(vector)
        main.append(previews)
        self.status = Gtk.Label(xalign=0, margin_start=10, margin_end=10, margin_top=4,
                                margin_bottom=4, label="No image loaded",
                                css_classes=["dim-label"], ellipsize=Pango.EllipsizeMode.END)
        # Long status lines must not widen the window; the full text is in the tooltip.
        self.status.bind_property("label", self.status, "tooltip-text")
        main.append(self.status)
        paned.set_end_child(main)

        for canvas in (self.orig_canvas, self.canvas):
            drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
            drop.connect("drop", self.on_drop)
            canvas.add_controller(drop)
        return sidebar

    def _build_presets(self, box):
        box.append(_heading("Preset"))
        grid = _grid()
        self.preset_btns = []
        for i, p in enumerate(P.PRESETS):
            btn = thumb_button(f"preset_{p.slug}", p.name, p.name, wrap=True)
            btn.connect("toggled", self._on_preset_clicked, i)
            grid.append(btn)
            self.preset_btns.append(btn)
        box.append(grid)
        self.custom_lbl = Gtk.Label(label="Custom settings", xalign=0, visible=False,
                                    css_classes=["dim-label"])
        box.append(self.custom_lbl)

    def _build_modes(self, box):
        box.append(_separator())
        box.append(_heading("Mode"))
        grid = _grid()
        self.mode_btns = []
        for i, m in enumerate(P.MODES):
            btn = thumb_button(f"mode_{m.id}", m.name, m.hint, wrap=False)
            btn.connect("toggled", self._on_mode_clicked, i)
            grid.append(btn)
            self.mode_btns.append(btn)
        box.append(grid)
        self.mode_hint = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "caption"])
        box.append(self.mode_hint)

    def _add(self, box, setting: Setting) -> Setting:
        self.settings[setting.name] = setting
        box.append(setting.row)
        return setting

    def _build_advanced(self, box):
        ch = self._on_setting_changed
        add = lambda s: self._add(box, s)  # noqa: E731
        d = te.TraceOptions()  # defaults

        box.append(_separator())
        box.append(_heading("Settings"))
        add(SliderSetting("threshold", "Threshold", 0.0, 1.0, 0.01, d.threshold, ch))
        add(SliderSetting("quant_colors", "Colors", 2, 32, 1, d.quant_colors, ch, digits=0))
        add(SliderSetting("scans", "Max colors", 2, 128, 1, d.scans, ch, digits=0))
        add(SliderSetting("stroke_width", "Stroke width (0 = auto)", 0.0, 30.0, 0.25,
                          d.stroke_width, ch))
        add(CheckSetting("invert", "Invert image", d.invert, ch))
        add(CheckSetting("smooth", "Smooth (blur before scanning)", d.smooth, ch))
        add(CheckSetting("stack", "Stack scans", d.stack, ch))
        add(CheckSetting("remove_background", "Remove background", d.remove_background, ch))
        add(SliderSetting("merge_de", "Merge similar colors (ΔE)", 1.0, 25.0, 0.5, d.merge_de,
                          ch, digits=1))
        add(EntrySetting("palette", "Fixed palette (optional, hex colors)", d.palette, ch,
                         placeholder="#1b1b1b, #e0c088, #5a7d3c"))
        add(CheckSetting("cleanup", "Clean up anti-aliasing colors (logos)", d.cleanup, ch))
        add(SliderSetting("merge_below", "Clean up colors under (% of image)", 0.0, 10.0, 0.1,
                          d.merge_below, ch, digits=1))

        box.append(_separator())
        box.append(_heading("Quality"))
        add(ChoiceSetting("upscale", "Upscale before tracing",
                          [("Auto", 0), ("1× (none)", 1), ("2×", 2), ("3×", 3), ("4×", 4)], ch))
        ai = te.ai_upscalers()
        add(ChoiceSetting("upscaler", "Upscaler",
                          [("Lanczos (fast)", "lanczos")] + [(f"{n} (AI, GPU)", n) for n in ai],
                          ch))
        if not ai:
            box.append(Gtk.Label(
                label="For AI upscaling install one of: waifu2x-ncnn-vulkan (pacman), "
                      "realesrgan-ncnn-vulkan or realcugan-ncnn-vulkan (AUR), then restart.",
                xalign=0, wrap=True, css_classes=["dim-label"], selectable=True))
        add(CheckSetting("keep_transparency", "Keep transparency", d.keep_transparency, ch))
        add(SliderSetting("edge_smooth", "Edge smoothing", 0.0, 3.0, 0.05, d.edge_smooth, ch))
        add(SliderSetting("denoise", "Denoise (JPEG noise)", 0.0, 3.0, 0.1, d.denoise, ch,
                          digits=1))

        box.append(_separator())
        box.append(_heading("Text (OCR)"))
        add(CheckSetting("ocr", "Recognize text → editable SVG text", d.ocr, ch))
        add(SliderSetting("ocr_min_conf", "Min. OCR confidence", 30, 95, 1, d.ocr_min_conf, ch,
                          digits=0))
        add(EntrySetting("font_family", "Font family", d.font_family, ch,
                         empty_value=te.DEFAULT_FONT))
        ok, msg = te.ocr_status()
        box.append(Gtk.Label(label=msg, xalign=0, wrap=True, css_classes=["dim-label"],
                             selectable=True, visible=not ok))

        box.append(_separator())
        box.append(_heading("Options"))
        add(SliderSetting("speckles", "Suppress speckles (px)", 0, 50, 1, d.speckles, ch,
                          digits=0))
        add(SliderSetting("noise_speckles", "Suppress low-contrast speckles (px)", 0, 50, 1,
                          d.noise_speckles, ch, digits=0))
        add(SliderSetting("smooth_corners", "Smooth corners", 0.0, 1.34, 0.01,
                          d.smooth_corners, ch))
        add(CheckSetting("optimize_enabled", "Optimize paths", d.optimize_enabled, ch))
        add(SliderSetting("optimize", "Optimize tolerance", 0.0, 5.0, 0.05, d.optimize, ch))

        box.append(_separator())
        self.live = Gtk.CheckButton(label="Live preview", active=True)
        box.append(self.live)
        update = Gtk.Button(label="Update preview")
        update.connect("clicked", lambda *_: self.retrace())
        box.append(update)

    # ---- state changes ----
    @contextlib.contextmanager
    def quiet(self):
        """Programmatic widget changes inside this block don't count as user
        edits (no 'custom' marking, no retrace)."""
        self._quiet_depth += 1
        try:
            yield
        finally:
            self._quiet_depth -= 1

    def _select_mode(self, idx):
        self.mode_idx = idx
        with self.quiet():
            for i, b in enumerate(self.mode_btns):
                b.set_active(i == idx)

    def _select_preset(self, idx):
        self.preset_idx = idx
        with self.quiet():
            for i, b in enumerate(self.preset_btns):
                b.set_active(i == idx)
        self.custom_lbl.set_visible(idx is None)

    def apply_preset(self, idx):
        preset = P.PRESETS[idx]
        with self.quiet():
            self._select_preset(idx)
            for name, value in preset.options.items():
                if name == "mode":
                    self._select_mode(P.mode_index(value))
                else:
                    self.settings[name].set(value)
        self._update_visibility()
        self.schedule()

    def _update_visibility(self):
        mode = P.MODES[self.mode_idx]
        self.mode_hint.set_label(mode.hint)
        for name, s in self.settings.items():
            visible = name not in P.MODE_DEPENDENT or name in mode.settings
            if name in P.REQUIRES:
                visible = visible and bool(self.settings[P.REQUIRES[name]].get())
            s.row.set_visible(visible)

    def _on_preset_clicked(self, _btn, idx):
        if not self._quiet_depth:
            self.apply_preset(idx)

    def _on_mode_clicked(self, _btn, idx):
        if self._quiet_depth:
            return
        self._select_mode(idx)
        self._select_preset(None)
        self._update_visibility()
        self.schedule()

    def _on_setting_changed(self, name):
        if self._quiet_depth:
            return
        self._select_preset(None)
        if name in P.REQUIRES.values():
            self._update_visibility()
        self.schedule()

    def _on_level_toggled(self, btn):
        advanced = btn.get_active()
        self.adv_box.set_visible(advanced)
        self.config["advanced"] = advanced
        save_config(self.config)

    def options(self) -> te.TraceOptions:
        return te.TraceOptions(mode=P.MODES[self.mode_idx].id,
                               **{name: s.get() for name, s in self.settings.items()})

    # ---- tracing ----
    def schedule(self):
        if not self.image or not self.live.get_active():
            return
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(DEBOUNCE_MS, self._debounced)

    def _debounced(self):
        self._debounce_id = 0
        self.retrace()
        return False

    def retrace(self):
        if not self.image:
            return
        self.worker.submit(self.image, self.options())
        self.spinner.start()
        self.status.set_label("Tracing…")

    def _trace_done(self, gen, res, err):
        if gen != self.worker.generation:
            return False  # a newer request is on its way
        self.spinner.stop()
        if err is not None:
            self.status.set_label(f"Error: {err}")
            return False
        self.result = res
        self.canvas.result = res
        self.canvas.queue_draw()
        has = res.path_count > 0 or bool(res.texts)
        self.export_btn.set_sensitive(has)
        self.copy_btn.set_sensitive(has)
        parts = [f"{os.path.basename(self.image_path)} — {res.width}×{res.height}px "
                 f"(traced at {res.scale}×)",
                 f"{len(res.layers)} colors, {res.path_count} paths, {res.node_count} nodes"
                 + (f", {len(res.texts)} text lines" if res.texts else "")]
        self.status.set_label(" — ".join(parts + res.warnings))
        return False

    # ---- file handling ----
    def load(self, path):
        try:
            img = te.load_image(path)
        except (OSError, ValueError) as e:
            self.status.set_label(f"Could not open {path}: {e}")
            return
        self.image, self.image_path = img, path
        self.result = None
        self.canvas.result = None
        self.canvas.set_original(img)
        self.orig_canvas.set_original(img)
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
                return  # cancelled
            if file:
                self.load(file.get_path())

        dlg.open(self, None, done)

    def on_export(self, *_):
        if not self.result:
            return
        dlg = Gtk.FileDialog(title="Export SVG")
        dlg.set_initial_name(os.path.splitext(os.path.basename(self.image_path))[0] + ".svg")
        dlg.set_initial_folder(Gio.File.new_for_path(os.path.dirname(self.image_path)))
        svg = self.result.to_svg()

        def done(d, r):
            try:
                file = d.save_finish(r)
            except GLib.Error:
                return  # cancelled
            if not file:
                return
            path = file.get_path()
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(svg)
            except OSError as e:
                self.status.set_label(f"Could not save {path}: {e.strerror}")
                Gtk.AlertDialog(message="Could not save the SVG",
                                detail=f"{path}\n\n{e.strerror or e}").show(self)
                return
            self.status.set_label(f"Saved {path}")

        dlg.save(self, None, done)

    def on_copy(self, *_):
        if self.result:
            self.get_clipboard().set(self.result.to_svg())
            self.status.set_label("SVG copied to clipboard")


SAMPLE = os.path.join(HERE, "assets", "samples", "ui.jpg")


class SelfTest:
    """`--self-test OUTDIR`: open the real window, trace the bundled sample with
    the OCR preset, save the SVG, a picture of the window and a report, then
    quit (exit code 1 on failure). Used to check packaged builds on every OS."""

    def __init__(self, app, outdir):
        self.app, self.outdir, self.ok = app, outdir, False
        self.deadline = GLib.get_monotonic_time() + 180 * 1_000_000

    def start(self, win):
        os.makedirs(self.outdir, exist_ok=True)
        win.load(SAMPLE)
        win.apply_preset(next(i for i, p in enumerate(P.PRESETS) if p.options.get("ocr")))
        GLib.timeout_add(500, self._poll, win)

    def _poll(self, win):
        done = win.result is not None and win.worker.generation and not win.spinner.get_spinning()
        if not done and GLib.get_monotonic_time() < self.deadline:
            return True
        self._finish(win)
        return False

    def _finish(self, win):
        import platform
        res = win.result
        ocr_ok, ocr_msg = te.ocr_status()
        lines = [f"platform: {platform.platform()}", f"python: {sys.version.split()[0]}",
                 f"gtk: {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}",
                 f"potrace: {te._lib}", f"ocr: {ocr_ok} ({ocr_msg})",
                 f"status: {win.status.get_label()}"]
        if res is not None:
            lines += [f"paths: {res.path_count}", f"texts: {[t.text for t in res.texts]}",
                      f"warnings: {res.warnings}"]
            with open(os.path.join(self.outdir, "self-test.svg"), "w", encoding="utf-8") as fh:
                fh.write(res.to_svg())
        self.ok = res is not None and res.path_count > 0 and (not ocr_ok or len(res.texts) > 0)
        lines.append(f"result: {'PASS' if self.ok else 'FAIL'}")
        with open(os.path.join(self.outdir, "self-test.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print("\n".join(lines), flush=True)
        try:  # picture of the window, rendered by GTK itself
            p = Gtk.WidgetPaintable.new(win)
            snap = Gtk.Snapshot()
            p.snapshot(snap, win.get_width(), win.get_height())
            node = snap.to_node()
            if node is not None:
                win.get_native().get_renderer().render_texture(node, None).save_to_png(
                    os.path.join(self.outdir, "self-test.png"))
        except Exception as e:  # noqa: BLE001 - a missing picture must not hide the result
            print(f"window snapshot failed: {e}", flush=True)
        self.app.quit()


class TracerApp(Gtk.Application):
    def __init__(self, self_test_dir=None):
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if self_test_dir:
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.self_test = SelfTest(self, self_test_dir) if self_test_dir else None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        display = Gdk.Display.get_default()
        Gtk.StyleContext.add_provider_for_display(display, css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        # App icon: works from the project folder too, before tools/install_desktop.py.
        Gtk.IconTheme.get_for_display(display).add_search_path(ICON_DIR)
        Gtk.Window.set_default_icon_name(APP_ID)

    def _window(self) -> TracerWindow:
        win = self.props.active_window or TracerWindow(self)
        win.present()
        return win

    def do_activate(self):
        win = self._window()
        if self.self_test:
            self.self_test.start(win)

    def do_open(self, files, _n, _hint):
        win = self._window()
        if files:
            win.load(files[0].get_path())


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if "--self-test" in argv:
        i = argv.index("--self-test")
        outdir = argv[i + 1] if i + 1 < len(argv) else "self-test-output"
        app = TracerApp(self_test_dir=outdir)
        app.run(argv[:1])
        return 0 if app.self_test.ok else 1
    return TracerApp().run(argv)


if __name__ == "__main__":
    sys.exit(main())
