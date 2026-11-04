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

## Download

Every release on the
[Releases page](https://github.com/sabaoongfx/oit-open-image-tracer/releases)
has these packages (the latest version is always at
`https://github.com/sabaoongfx/oit-open-image-tracer/releases/latest/download/<name>`):

| System | File | Install |
|---|---|---|
| Any Linux (Flatpak) | `Open-Image-Tracer-x86_64.flatpak` | `flatpak install --user Open-Image-Tracer-x86_64.flatpak` |
| Any Linux (AppImage) | `Open-Image-Tracer-x86_64.AppImage` | `chmod +x` it and run it |
| Debian 13+ / Ubuntu 24.04+ | `Open-Image-Tracer-all.deb` | `sudo apt install ./Open-Image-Tracer-all.deb` |
| Fedora | `Open-Image-Tracer-noarch.rpm` | `sudo dnf install ./Open-Image-Tracer-noarch.rpm` |
| Arch / Manjaro | `Open-Image-Tracer-any.pkg.tar.zst` | `sudo pacman -U Open-Image-Tracer-any.pkg.tar.zst` |
| Windows 10/11 | `Open-Image-Tracer-x64-setup.exe` or `.msi` | run the installer |
| macOS (Apple Silicon / Intel) | `Open-Image-Tracer-aarch64.dmg` / `-x64.dmg` | open, drag to Applications (unsigned: right-click → Open the first time) |

`SHA256SUMS` lists the checksums.

## Packaging and releases

All packaging lives in `packaging/`: a shared Linux install script
(`packaging/linux/install.sh`) used by the .deb, .rpm, pacman and Flatpak
builds; a PyInstaller spec shared by the AppImage, Windows and macOS builds;
and per-format build scripts. `.github/workflows/release.yml` builds every
format, installs/runs each one with `--self-test` (traces a sample with OCR
and saves a picture of the window), and on a `v*` tag matching `VERSION`
attaches everything to a draft release.

To release: bump `VERSION`, `pkgver` in `packaging/arch/PKGBUILD`, `Version`
in `packaging/rpm/open-image-tracer.spec` and add a `<release>` to the
metainfo; commit, then `git tag vX.Y.Z && git push origin vX.Y.Z`, review the
draft release and publish it.

Build the Flatpak locally:

```sh
flatpak install --user flathub org.flatpak.Builder org.gnome.Sdk//51
flatpak run org.flatpak.Builder --user --install --force-clean \
    ~/.cache/oit-flatpak/build packaging/flatpak/io.github.sabaoongfx.OpenImageTracer.yml
```

`python3 image_tracer.py --self-test OUTDIR` checks any installation.

## Install the app icon and launcher (running from source)

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

## License

Image Tracer is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. See [LICENSE](LICENSE). It uses potrace (GPL-2.0-or-later) and
Tesseract (Apache-2.0).
