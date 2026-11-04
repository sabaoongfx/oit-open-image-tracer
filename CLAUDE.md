# Image Tracer — notes for Claude

Standalone GTK4 desktop app that converts bitmaps to SVG (like Inkscape's Trace
Bitmap). Python 3.14 on Manjaro/KDE; no scipy/OpenCV/pytest — only numpy,
Pillow, PyGObject, pycairo, libpotrace (ctypes). See README.md for layout and
requirements.

## Commands

```sh
python3 image_tracer.py [image]                  # run the app
python3 -m unittest discover -s tests -v         # 20 tests (OCR/GUI auto-skip if unavailable)
UPDATE_GOLDEN=1 python3 -m unittest discover -s tests   # only after an intended output change
python3 tools/make_thumbs.py                     # after editing presets.py / modes
python3 tools/gradient_trace.py in.png out.svg   # experimental gradient vectorizer (WIP)
```

## Architecture (single sources of truth)

- `trace_engine.py` — engine, no GUI. `trace(img, TraceOptions, cancelled)`;
  one function per mode in `_MODE_FUNCS`; tuning constants grouped at the top;
  raises `Cancelled` between stages. Caches are `_Memo` + `_digest` (full hash).
- `presets.py` — modes (with the settings each uses: `ModeSpec.settings`,
  `MODE_DEPENDENT`, `REQUIRES`) and presets (`PresetSpec`, option keys are
  `TraceOptions` field names). Thumbnails are `assets/thumbs/mode_<id>.png`
  and `preset_<slug>.png`.
- `image_tracer.py` — GUI. Each setting is a `Setting` subclass bound to one
  `TraceOptions` field (`self.settings[name]`); `options()` and `apply_preset()`
  go through that dict. Programmatic changes go inside `with self.quiet():`.
  One `TraceWorker` thread; new requests cancel the running trace.
- `centerline.py` — Line drawing mode (returns `trace_engine.Contour`).

## Conventions

- Verify output changes visually: render with `rsvg-convert` and compare
  against the source (and against the user's vectorizer.com / Adobe exports
  in ~/Downloads when relevant). The user judges by eye.
- Engine refactors must keep `tests/golden.json` identical; a golden change
  needs a reason.
- GUI screenshots: never `spectacle -a` (captures whatever window is active,
  e.g. the user's terminals). Render the window from inside GTK with
  `Gtk.WidgetPaintable` + `render_texture`, using
  `Gio.ApplicationFlags.NON_UNIQUE` and a temporary `XDG_CONFIG_HOME`
  (restore the user's `~/.config/image-tracer/config.json` if touched).
- The user keeps the app open; restart it after changes with
  `kill $(pgrep -f "^python3 image_tracer.py")` then relaunch detached
  (don't `pkill -f` a pattern that matches your own shell).
- Commit messages end with the Co-Authored-By line from the session.

## UI decisions the user made

- Simple mode = Preset grid (top) + Mode grid only; everything else in Advanced.
- Mode/preset buttons use fixed example thumbnails, not traces of the user's image.
- Mode names: Full color, Black & white, Line drawing, Outlines, Grayscale,
  Gray steps, Posterize (each with a hint under the grid).
- Sliders with a typeable value field instead of +/- spin buttons.
- Previews side by side: "Image" (left) and "Vector" (right), equal halves.

## In progress: SVG of the user's logo (paused 2026-11-04)

Goal: a clean SVG of `~/Downloads/Open Image Tracer Logo.png` (1254², RGBA,
gradients + soft lavender glow around the photo card). The user's own export
from the app (`Open Image Tracer Logo.svg`, 1.9 MB) has banded gradients —
don't overwrite it; write a new file next to it.

Approach: `tools/gradient_trace.py` (uncommitted) — edge-aware regions
(Lab Sobel > `EDGE`), run-based connected components, per-region linear
gradient fit (`<linearGradient>` with 6 stops) or flat fill, smart stacking via
engine helpers, wide semi-transparent bands → one blurred glow shape.

State:
- Best so far: `EDGE = 3.5`, *without* `SMOOTH` silhouette smoothing and
  without `_recover_details` → 44 KB, 48 paths, 31 gradients, mean color error
  3.82 (user's 1.9 MB SVG: 4.08).
- Remaining flaws there: torn outer edge of the photo card's white border
  (near-opaque lavender pixels between glow and border; the card itself is only
  ~99% alpha) and the thin Bézier curve on the document stops short of its
  square handles.
- The last attempt (`SMOOTH` + `_recover_details`, currently in the file)
  reconnected the curve but added edge speckles and a dotted border (299 paths)
  — rework or remove both before continuing.

Next steps: fix those two flaws, save the final SVG beside the PNG, then offer
to add gradient tracing to the app as a "Smooth gradients" option.
