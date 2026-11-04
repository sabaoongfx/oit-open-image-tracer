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

## Gradient vectorizer (tools/gradient_trace.py)

Built for the user's logo (`~/Downloads/Open Image Tracer Logo.png`, gradients
+ soft glow). Delivered `~/Downloads/Open Image Tracer Logo (vector).svg`:
48 KB, 53 paths, 32 linear gradients, mean color error 3.73 vs the PNG (the
user's own banded 1.9 MB export: 4.08). Never overwrite the user's files.

How it works: edge-aware regions (Lab Sobel > `EDGE` = 3.5; real gradients
change ~0.2/px, edges are sharp steps), run-based connected components,
per-region linear gradient (6 stops) or flat fill, `_recover_details` for
hairlines (far from region color *and* ≥3 px thick), smart stacking via engine
helpers. Glow = semi-transparent bands ≥ `GLOW_BAND` px wide (thin ones are
anti-aliasing), plus glow-colored near-opaque rim pixels; strongly colored
semi-transparent pixels in the band are objects (an arc passing behind the
card). Rendered as one blurred shape at the bottom.

Tried and rejected: silhouette open/close smoothing (bridges letter gaps),
drawing the glow between "behind" and "front" regions (detection broke the glow).

Known limits: soft semi-transparent blends (arc fading into the glow, the
border's lower edge) come out as slightly hard-edged lavender areas.

Also delivered `~/Downloads/Image-to-SVG Converter Icon (vector).svg` (26 KB,
14 gradients): the user chose a clean background (one gradient, no swooshes or
drop shadows) over approximating the painted background, which looked cloudy.
Opt-in experiments kept in the tool (env vars, default off): `SOFT_MODE`
(blurred soft shapes for shading), `COARSE` (edges at 1/N resolution for soft
borders), `MERGE_EDGE` (merge regions with weak borders). Detail recovery
skips areas wider than `DETAIL_MAX_WIDTH` (those are shading, e.g. shadows).

Possible next step: offer it in the app as a "Smooth gradients" option for
logos/illustrations (would need its own golden tests).
