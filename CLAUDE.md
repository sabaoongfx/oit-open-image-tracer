# Open Image Tracer — notes for Claude

Standalone GTK 4 desktop app that converts bitmaps to SVG (like Inkscape's
Trace Bitmap). Python 3.14 on Manjaro/KDE (Wayland); dependencies are only
numpy, Pillow, PyGObject, pycairo and libpotrace (ctypes), optional tesseract.
No scipy/OpenCV/pytest on the machine. Repo: github.com/sabaoongfx/oit-open-image-tracer
(public, branch `main`). License GPL-3.0-or-later.

Display name: **Open Image Tracer** (window title, menu entry, installers,
macOS app, docs). Internal names stay: app id
`io.github.sabaoongfx.OpenImageTracer`, package/command `open-image-tracer`,
Windows registry key `OpenImageTracer`, and the signing key's UID
**"Image Tracer packages"** (users type it in `pacman-key --lsign-key`; renaming
it would need a new key).

## Ground rules from the user

- **Don't start CI runs, push tags or create/publish releases unless asked**
  ("we will run later"). Pushing commits to `main` is fine when asked to push.
- Never overwrite the user's files in ~/Downloads; write new files next to them.
- The user judges results by eye: render SVGs with `rsvg-convert` and compare
  against the source (and the user's vectorizer.com / Adobe exports) before
  reporting.
- Flathub: never open a submission PR or write a manifest for submission (see
  "Flathub" below).

## Commands

```sh
python3 image_tracer.py [image]                  # run from source
python3 image_tracer.py --self-test OUTDIR       # open window, trace sample with OCR, save
                                                 #   report + SVG + window picture (exit 1 = fail)
python3 -m unittest discover -s tests -v         # 20 tests (OCR/GUI skip if unavailable)
UPDATE_GOLDEN=1 python3 -m unittest discover -s tests   # only after an intended output change
python3 tools/make_thumbs.py                     # mode/preset thumbnails, after editing presets.py
python3 tools/make_app_icon.py                   # icon set from assets/icon-source.* (now .jpg)
python3 tools/install_desktop.py [--uninstall]   # native user launcher + icon (not installed now)
python3 tools/gradient_trace.py in.png out.svg   # gradient vectorizer for logos/icons
flatpak run org.flatpak.Builder --user --install --force-clean \
    ~/.cache/oit-flatpak/build packaging/flatpak/io.github.sabaoongfx.OpenImageTracer.yml
gh workflow run release.yml --ref main           # CI: build + self-test all formats (ask first!)
```

## Architecture (single sources of truth)

- `trace_engine.py` — engine, no GUI. `trace(img, TraceOptions, cancelled)`;
  one function per mode in `_MODE_FUNCS`; tuning constants grouped at the top;
  raises `Cancelled` between stages. Caches are `_Memo` + `_digest` (full hash).
  Cross-platform: `_load_potrace()` (Linux/Windows/macOS names, bundle dir),
  `_tool()` finds tesseract/fc-match in a PyInstaller bundle first,
  `_tesseract_env()` points a bundled tesseract at bundled `tessdata/`.
- `presets.py` — modes (`ModeSpec.settings`, `MODE_DEPENDENT`, `REQUIRES`) and
  presets (`PresetSpec`; option keys are `TraceOptions` field names). GTK-free.
- `image_tracer.py` — GUI. Each setting is a `Setting` subclass bound to one
  `TraceOptions` field (`self.settings[name]`); `options()`/`apply_preset()` go
  through that dict. Programmatic changes inside `with self.quiet():`. One
  `TraceWorker` thread; new requests cancel the running one. `HERE` is the
  source dir or `sys._MEIPASS`; config via `GLib.get_user_config_dir()`.
  `SelfTest` implements `--self-test`.
- `centerline.py` — Line drawing mode (returns `trace_engine.Contour`).
- `APP_ID = "io.github.sabaoongfx.OpenImageTracer"` names the icon files, the
  .desktop file, metainfo and the Flatpak.

## Packaging (packaging/)

- `linux/` — launcher (`open-image-tracer`, works under /usr, /app, AppImage),
  desktop entry, metainfo, and `install.sh DESTDIR PREFIX`, shared by the
  deb, rpm, pacman and Flatpak builds.
- `arch/PKGBUILD`, `deb/build-deb.sh`, `rpm/open-image-tracer.spec` (each with
  its distro's dependency names; tesseract optional/recommended).
- `flatpak/` manifest: GNOME 51 runtime; bundles potrace, leptonica,
  tesseract 5.5.3 + tessdata `eng` 4.1.0 (identical to the system's), numpy,
  Pillow (pinned wheels); `TESSDATA_PREFIX` set; no filesystem/network access
  (portals). Host AI upscalers are not reachable from the sandbox.
- `pyinstaller/open-image-tracer.spec` — shared by AppImage, Windows, macOS;
  build scripts pass `POTRACE_LIB`, `TESSERACT`, `TESSDATA` (eng model +
  `configs/`, which holds the `tsv` output format OCR needs), `APP_ICON`.
- `appimage/build-appimage.sh` (Ubuntu 24.04), `windows/build.sh` (MSYS2
  UCRT64) + `installer.nsi` (NSIS) + `installer.wxs` (WiX 5 MSI),
  `macos/build.sh` (Homebrew; .app + .dmg; unsigned).
- Version: `VERSION`, plus `pkgver` in PKGBUILD, `Version` in the spec and a
  `<release>` in the metainfo — keep them in sync.

## CI and releases

`.github/workflows/release.yml` (on `v*` tags and workflow_dispatch) builds
deb, rpm, pacman, Flatpak, AppImage, Windows (setup.exe + MSI) and macOS
(macos-latest = arm64, macos-15-intel), installs/runs each with `--self-test`
under xvfb/`GSK_RENDERER=cairo` (artifacts `self-test-*` contain the report and
a window picture — look at them), and on a tag creates a **draft** release with
all files, versionless copies (`Open-Image-Tracer-x86_64.AppImage`, …-all.deb,
…-noarch.rpm, …-any.pkg.tar.zst, …-x64-setup.exe, …-x64.msi, …-aarch64.dmg,
…-x64.dmg, …-x86_64.flatpak; for `releases/latest/download/<name>`) and
`SHA256SUMS`. Then check sizes/checksums and publish with
`gh release edit vX --draft=false --latest`.

GitHub flakiness seen: macOS arm64 jobs sometimes never get a runner ("capacity
constraints") and get cancelled; the Flatpak job once failed with "Invalid
cross-device link" during an Actions outage. Both are infra issues — rerun with
`gh run rerun <id> --failed`. A cancelled queued run can stay "queued" for a
while even after force-cancel.

Release state:
- v0.1.0 — Flatpak only (published).
- v0.1.1 — published 2026-11-04: Flatpak, AppImage, deb, rpm, pacman, Windows
  setup.exe, macOS dmg arm64 + Intel; all CI self-tested with OCR. Its MSI was
  empty (172 KB: WiX resolved a relative Source path from the .wxs folder) and
  was removed before publishing; release notes promise an MSI "shortly".
- Fixed on `main` (commit 3b5a774, not yet run): absolute WiX paths, build
  fails if the MSI is < 20 MB, MSI is installed on the runner and the
  installed app self-tested. **Next (when the user says so):** run the
  workflow, download `pkg-windows`, attach the MSI (versioned + versionless) to
  v0.1.1 and add both to its SHA256SUMS.

Signed apt + pacman repos (like Open Task Manager/notz): GitHub Pages of this
repo, https://sabaoongfx.github.io/oit-open-image-tracer/ (`/KEY.gpg`,
`dists/stable`, `/arch/{x86_64,aarch64}/open-image-tracer.db`). Built by
`.github/workflows/publish-linux.yml` on release *published* (or
`gh workflow run publish-linux.yml -f tag=vX.Y.Z`): re-signs the release's
.deb (reprepro, `packaging/apt/distributions`) and Arch package
(`packaging/arch/make-repo.sh`), deploys Pages, then `verify-arch` /
`verify-apt` install from the live repos and run `--self-test`. The `github-pages` environment allows
deployments from branch `main` and tags `v*` (added after v0.1.2's publish was
rejected: a release runs on its tag ref). Signing key
"Image Tracer packages" (rsa4096, no passphrase, fingerprint
0BB3344EA53F9F3457D421EFE8E503CE9A824E81): private key in the user's local
keyring (revocation cert in ~/.gnupg/openpgp-revocs.d/) and as the
`PACKAGES_GPG_PRIVATE_KEY` secret. Locally, several secret keys exist (notz,
OTM, VLC), so make-repo.sh run by hand should get `GPGKEY=<fpr>`.

On this machine: the Flatpak is installed (user, from a local v0.1.0 bundle,
origin `openimagetracer-origin`); the native source launcher is uninstalled.

## Flathub

Flathub's Generative AI policy (Requirements → Inclusion policy): AI tools
must not open or automate submission PRs or write their text/replies;
Flathub manifests must not contain AI-generated or AI-assisted content;
AI-generated app code must be disclosed. So never open a Flathub PR or write
a manifest for submission; `packaging/flatpak/` is for our own builds. A
submission would also need the ID to match the repo
(`io.github.sabaoongfx.oit-open-image-tracer`) and screenshots in the metainfo.

## Conventions

- Engine refactors must keep `tests/golden.json` identical; a golden change
  needs a reason.
- GUI screenshots: never `spectacle -a` (it captured the user's terminals).
  Use `--self-test` or render the window inside GTK with `Gtk.WidgetPaintable`
  + `render_texture`, `Gio.ApplicationFlags.NON_UNIQUE` and a temporary
  `XDG_CONFIG_HOME` (restore `~/.config/image-tracer/config.json` if touched).
- Restarting a running source instance: `kill $(pgrep -f "^python3 image_tracer.py")`
  — never a pattern that also matches your own shell command.
- Commit as `Sabaoon <sabaoongfx@gmail.com>`; messages end with the
  Co-Authored-By line from the session.

## UI decisions the user made

- Simple mode = Preset grid (top) + Mode grid only; everything else in
  Advanced (choice remembered in config).
- Mode/preset buttons use fixed example thumbnails, not traces of the user's image.
- Mode names: Full color, Black & white, Line drawing, Outlines, Grayscale,
  Gray steps, Posterize (hint under the grid).
- Sliders with a typeable value field instead of +/- spin buttons.
- Previews side by side: "Image" (left) and "Vector" (right), equal halves.
- No custom header bar: the OS draws the title bar (on KDE Wayland GTK then
  uses server-side decorations; no `csd` class; on Windows `GTK_CSD=0` is set
  at startup, since GTK 4 otherwise draws its own title bar there — seen in
  the CI self-test picture). Buttons live in a toolbar
  row inside the window. Don't style the top of the window.
- Tracing progress: `trace(..., progress=cb)` reports (fraction, label) per
  stage and per layer; the status row shows a gradient progress bar
  (purple → blue → green → orange, `PROGRESS_STOPS`) and a bold percentage
  in the matching color; the Vector pane shows the same during the first trace.
- App icon: the pen-and-pixels design (`assets/icon-source.jpg`, white
  background made transparent by `make_app_icon.py`).

## Gradient vectorizer (tools/gradient_trace.py)

Edge-aware regions (Lab Sobel > `EDGE` = 3.5; real gradients change ~0.2/px),
run-based connected components, per-region linear gradient (6 stops) or flat
fill, `_recover_details` for thin hairlines (skips areas wider than
`DETAIL_MAX_WIDTH` — that's shading), smart stacking via engine helpers.
Glows = semi-transparent bands ≥ `GLOW_BAND` px wide, rendered as one blurred
shape. Opt-in experiments (env, default off): `SOFT_MODE`, `COARSE`,
`MERGE_EDGE`.

Delivered: `~/Downloads/Open Image Tracer Logo (vector).svg` (48 KB, 32
gradients, error 3.73 vs the user's banded 1.9 MB export at 4.08) and
`~/Downloads/Image-to-SVG Converter Icon (vector).svg` (26 KB; the user chose a
clean one-gradient background over approximating the painted swooshes).
Known limit: soft semi-transparent blends come out slightly hard-edged.
Possible next step: offer it in the app as a "Smooth gradients" option.
