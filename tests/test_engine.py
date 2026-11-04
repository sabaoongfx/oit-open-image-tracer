"""Regression tests. Run from the project folder:

    python3 -m unittest discover -s tests -v

Golden results for the sample images live in tests/golden.json; after an
intended change in output, regenerate them with

    UPDATE_GOLDEN=1 python3 -m unittest discover -s tests
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import unittest
import xml.etree.ElementTree as ET

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import presets as P  # noqa: E402
import trace_engine as te  # noqa: E402

SAMPLES = os.path.join(ROOT, "assets", "samples")
THUMBS = os.path.join(ROOT, "assets", "thumbs")
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden.json")
FIELDS = {f.name for f in dataclasses.fields(te.TraceOptions)}


def sample(name):
    return te.load_image(os.path.join(SAMPLES, name))


def summary(result: te.TraceResult) -> dict:
    return {"colors": ["#%02x%02x%02x" % layer.color for layer in result.layers],
            "paths": result.path_count, "nodes": result.node_count,
            "texts": [t.text for t in result.texts]}


def golden_cases():
    """name -> (sample file, TraceOptions)."""
    cases = {f"mode_{m.id}": ("cartoon.png", te.TraceOptions(mode=m.id)) for m in P.MODES}
    for p in P.PRESETS:
        if p.options.get("ocr"):
            continue  # OCR output depends on the installed tesseract; tested separately
        file = {"logo": "logo.png", "ui": "ui.jpg", "sketch": "sketch.png"}[p.sample]
        cases[f"preset_{p.slug}"] = (file, te.TraceOptions(**p.options))
    cases["palette"] = ("logo.png", te.TraceOptions(palette="#000, #fff, #2d6cdf"))
    cases["remove_background"] = ("cartoon.png", te.TraceOptions(remove_background=True))
    cases["no_stack"] = ("cartoon.png", te.TraceOptions(stack=False))
    return cases


class PresetConsistency(unittest.TestCase):
    def test_mode_ids_match_engine(self):
        self.assertEqual([m.id for m in P.MODES], list(te.MODE_IDS))

    def test_preset_options_are_engine_fields(self):
        for p in P.PRESETS:
            self.assertLessEqual(set(p.options), FIELDS, p.name)
            te.TraceOptions(**p.options)  # constructible

    def test_mode_settings_are_engine_fields(self):
        for m in P.MODES:
            self.assertLessEqual(set(m.settings), FIELDS, m.name)
        self.assertLessEqual(set(P.REQUIRES) | set(P.REQUIRES.values()), FIELDS)

    def test_every_option_has_a_thumbnail(self):
        names = [f"mode_{m.id}" for m in P.MODES] + [f"preset_{p.slug}" for p in P.PRESETS]
        for n in names:
            self.assertTrue(os.path.exists(os.path.join(THUMBS, n + ".png")), n)


class Golden(unittest.TestCase):
    def test_golden_results(self):
        got = {}
        for name, (file, opts) in golden_cases().items():
            res = te.trace(sample(file), opts)
            ET.fromstring(res.to_svg())  # well-formed SVG
            got[name] = summary(res)
        if os.environ.get("UPDATE_GOLDEN") or not os.path.exists(GOLDEN):
            with open(GOLDEN, "w", encoding="utf-8") as fh:
                json.dump(got, fh, indent=1)
            self.skipTest("golden results written")
        with open(GOLDEN, encoding="utf-8") as fh:
            want = json.load(fh)
        for name in want:
            with self.subTest(case=name):
                self.assertEqual(got[name], want[name])


class Engine(unittest.TestCase):
    def test_parse_palette(self):
        self.assertEqual(te.parse_palette("#1b1b1b, e0c088 #fff; 12345 #GGG"),
                         [(27, 27, 27), (224, 192, 136), (255, 255, 255)])

    def test_cancel_raises(self):
        with self.assertRaises(te.Cancelled):
            te.trace(sample("cartoon.png"), te.TraceOptions(), cancelled=lambda: True)

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            te.trace(sample("cartoon.png"), te.TraceOptions(mode="nope"))

    def test_cache_key_sees_every_pixel(self):
        a = np.zeros((70, 70, 3), np.uint8)
        b = a.copy()
        b[1, 1] = 255  # a pixel the old every-7th-pixel key never looked at
        self.assertNotEqual(te._digest(a), te._digest(b))

    def test_memo(self):
        m = te._Memo()
        m.put("k", 1)
        self.assertEqual(m.get("k"), 1)
        self.assertIsNone(m.get("other"))

    def test_compact_path(self):
        layer = te.Layer((0, 0, 0), [te.Contour((1.0, 2.0), [("L", (3.0, 2.0)),
                                                             ("C", (3.0, 4.0, 1.0, 4.0, 1.0, 2.0))])])
        self.assertEqual(layer.svg_d(), "M1 2l2 0c0 2-2 2-2 0z")

    def test_open_contour_has_no_close(self):
        layer = te.Layer((0, 0, 0), [te.Contour((0.0, 0.0), [("L", (5.0, 0.0))], closed=False)],
                         stroke_width=2)
        self.assertNotIn("z", layer.svg_d())
        self.assertIn('fill="none"', te.TraceResult(10, 10, [layer]).to_svg())

    def test_unit_spaces(self):
        ink = np.zeros((10, 40), bool)
        ink[:, 0:6] = True  # "0"
        ink[:, 10:30] = True  # "MB" after a word-sized gap
        self.assertEqual(te._fix_unit_spaces("0MB", ink), "0 MB")
        tight = np.ones((10, 30), bool)
        self.assertEqual(te._fix_unit_spaces("4K", tight), "4K")

    def test_missing_ai_upscaler_warns(self):
        res = te.trace(sample("logo.png"), te.TraceOptions(upscale=2, upscaler="no-such-model"))
        self.assertTrue(any("not installed" in w for w in res.warnings))

    def test_transparency_kept(self):
        # Bottom layer is the blue square, not a white backdrop for the transparent area.
        kept = te.trace(sample("logo.png"), te.TraceOptions())
        self.assertEqual(summary(kept)["colors"][0], "#2d6cdf")
        flat = te.trace(sample("logo.png"), te.TraceOptions(keep_transparency=False))
        self.assertEqual(summary(flat)["colors"][0], "#ffffff")


@unittest.skipUnless(te.ocr_available(), "tesseract with English data not installed")
class Ocr(unittest.TestCase):
    def test_screenshot_text(self):
        preset = next(p for p in P.PRESETS if p.options.get("ocr"))
        res = te.trace(sample("ui.jpg"), te.TraceOptions(**preset.options))
        texts = " | ".join(t.text for t in res.texts)
        for word in ("Task Manager", "App history", "Delete usage history", "Service", "13.5 MB"):
            self.assertIn(word, texts)
        ET.fromstring(res.to_svg())


def _display_available():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        return Gtk.init_check()
    except (ImportError, ValueError):
        return False


@unittest.skipUnless(_display_available(), "no display for GTK")
class Gui(unittest.TestCase):
    """Presets drive the widgets, and the widgets produce the preset's options."""

    @classmethod
    def setUpClass(cls):
        os.environ["XDG_CONFIG_HOME"] = os.path.join(ROOT, "tests", ".config-tmp")
        import image_tracer
        from gi.repository import Gio
        cls.app = image_tracer.TracerApp()
        cls.app.set_flags(cls.app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
        cls.app.register(None)
        cls.win = image_tracer.TracerWindow(cls.app)

    def test_presets_round_trip(self):
        for i, p in enumerate(P.PRESETS):
            self.win.apply_preset(i)
            opts = self.win.options()
            for name, value in p.options.items():
                self.assertEqual(getattr(opts, name), value, f"{p.name}: {name}")
            self.assertEqual(self.win.preset_idx, i)

    def test_hand_change_means_custom(self):
        self.win.apply_preset(0)
        self.win.settings["scans"].set(20)  # emits like a user edit
        self.assertIsNone(self.win.preset_idx)
        self.assertEqual(self.win.options().scans, 20)

    def test_mode_visibility(self):
        self.win.apply_preset(0)  # full color
        self.assertTrue(self.win.settings["scans"].row.get_visible())
        self.assertFalse(self.win.settings["threshold"].row.get_visible())
        self.win.mode_btns[P.mode_index("brightness")].set_active(True)
        self.assertTrue(self.win.settings["threshold"].row.get_visible())
        self.assertFalse(self.win.settings["scans"].row.get_visible())

    def test_typed_value_is_clamped(self):
        s = self.win.settings["scans"]
        s.entry.set_text("999")
        s.entry.emit("activate")
        self.assertEqual(s.get(), 128)
        s.entry.set_text("abc")
        s.entry.emit("activate")
        self.assertEqual(s.entry.get_text(), "128")


if __name__ == "__main__":
    unittest.main()
