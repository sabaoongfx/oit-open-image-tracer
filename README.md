# Image Tracer

A small desktop app that does one thing: convert a bitmap (PNG, JPEG, …) into an
SVG vector, like Inkscape's *Trace Bitmap*, plus screenshot-friendly extras:
perceptual color palettes, JPEG denoising, OCR text as real `<text>`, centerline
tracing for line art and optional AI upscaling.

## Run

```sh
python3 image_tracer.py [image]
```

**Simple** shows only presets and modes; **Advanced** shows every setting.
Each slider has a field next to it for typing exact values.

## Install the app icon and launcher

```sh
python3 tools/install_desktop.py            # menu entry + icon for your user (no root)
python3 tools/install_desktop.py --uninstall
```

The icon set in `assets/icons/` is generated from `assets/icon-source.png` by
`tools/make_app_icon.py` (black corners become transparent).

## Requirements (Arch / Manjaro package names)

| Needed for | Packages |
|---|---|
| The app | `python` (3.10+), `python-gobject`, `gtk4`, `python-cairo`, `python-numpy`, `python-pillow`, `potrace` |
| Rendering SVGs in `tools/make_thumbs.py` | `librsvg` (`rsvg-convert`) |
| OCR (optional) | `tesseract`, `tesseract-data-eng` |
| AI upscaling (optional, Vulkan GPU) | `waifu2x-ncnn-vulkan` (repo), or `realesrgan-ncnn-vulkan` / `realcugan-ncnn-vulkan` (AUR) |

## Project layout

| File | What it is |
|---|---|
| `trace_engine.py` | Tracing engine: preprocessing, palette, potrace (via ctypes), OCR, SVG output. No GUI. |
| `centerline.py` | Line art → single strokes (thinning, skeleton graph, curve fitting). |
| `presets.py` | Modes and presets shown in the app (single source for GUI and tools). |
| `image_tracer.py` | GTK4 user interface. |
| `tools/make_thumbs.py` | Draws the sample images and renders the mode/preset thumbnails into `assets/`. |
| `tests/test_engine.py` | Regression tests (engine, presets, OCR, GUI). |

## Tests

```sh
python3 -m unittest discover -s tests -v
```

`tests/golden.json` holds the expected results for the sample images. After a
change that is *meant* to alter the output, regenerate it with
`UPDATE_GOLDEN=1 python3 -m unittest discover -s tests` and review the diff.
OCR and GUI tests are skipped automatically when tesseract or a display is
missing.

## Changing modes or presets

Edit `presets.py`, then regenerate the thumbnails:

```sh
python3 tools/make_thumbs.py
```
