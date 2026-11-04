#!/usr/bin/env python3
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

HERE = os.path.dirname(os.path.abspath(__file__))
THUMB_DIR = os.path.join(HERE, "assets", "thumbs")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
                      "image-tracer", "config.json")
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
    """Draws either the traced vectors or the original bitmap, fit to view."""
    PAD = 16

    def __init__(self):
        super().__init__(hexpand=True, vexpand=True)
        self.result: te.TraceResult | None = None
        self.surface: cairo.ImageSurface | None = None  # original bitmap
        self.show_original = False
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
        if self.show_original or self.result is None:
            cr.set_source_surface(self.surface, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
        else:
            self._draw_result(cr, self.result)

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

        view = Gtk.Box(css_classes=["linked"])
        self.vec_toggle = Gtk.ToggleButton(label="Vector", active=True)
        orig_toggle = Gtk.ToggleButton(label="Original", group=self.vec_toggle)
        self.vec_toggle.connect("toggled", self.on_view_toggled)
        view.append(self.vec_toggle)
        view.append(orig_toggle)
        header.set_title_widget(view)

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
        self.canvas = Canvas()
        main.append(self.canvas)
        self.status = Gtk.Label(xalign=0, margin_start=10, margin_end=10, margin_top=4,
                                margin_bottom=4, label="No image loaded",
                                css_classes=["dim-label"])
        main.append(self.status)
        paned.set_end_child(main)

        drop = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop.connect("drop", self.on_drop)
        self.canvas.add_controller(drop)
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

    def on_view_toggled(self, *_):
        self.canvas.show_original = not self.vec_toggle.get_active()
        self.canvas.queue_draw()


class TracerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="local.imagetracer",
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _window(self) -> TracerWindow:
        win = self.props.active_window or TracerWindow(self)
        win.present()
        return win

    def do_activate(self):
        self._window()

    def do_open(self, files, _n, _hint):
        win = self._window()
        if files:
            win.load(files[0].get_path())


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(TracerApp().run(sys.argv))
