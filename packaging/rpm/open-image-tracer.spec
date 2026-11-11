# SPDX-License-Identifier: GPL-3.0-or-later
Name:           open-image-tracer
Version:        0.1.3
Release:        1%{?dist}
Summary:        Convert images into SVG vectors
License:        GPL-3.0-or-later
URL:            https://github.com/sabaoongfx/oit-open-image-tracer
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       python3 >= 3.10
Requires:       python3-gobject
Requires:       gtk4 >= 4.10
Requires:       python3-cairo
Requires:       python3-numpy
Requires:       python3-pillow
Requires:       potrace
Requires:       fontconfig
Recommends:     tesseract
Recommends:     tesseract-langpack-eng

%description
Open Image Tracer turns bitmaps (PNG, JPEG, ...) into scalable SVG vectors, with
presets for logos, screenshots and line art, OCR for real SVG text, and a
side-by-side preview.

%prep
%autosetup -n oit-open-image-tracer-%{version}

%build
# nothing to build: pure Python

%install
./packaging/linux/install.sh %{buildroot} %{_prefix}

%files
%license LICENSE
%{_bindir}/open-image-tracer
%{_datadir}/open-image-tracer/
%{_datadir}/applications/io.github.sabaoongfx.OpenImageTracer.desktop
%{_datadir}/metainfo/io.github.sabaoongfx.OpenImageTracer.metainfo.xml
%{_datadir}/icons/hicolor/*/apps/io.github.sabaoongfx.OpenImageTracer.png
%exclude %{_datadir}/licenses/open-image-tracer/LICENSE

%changelog
* Wed Nov 11 2026 Sabaoon <sabaoongfx@gmail.com> - 0.1.3-1
- Drop-zone first screen; tracing progress card; no console windows on Windows
* Thu Nov 05 2026 Sabaoon <sabaoongfx@gmail.com> - 0.1.2-1
- Renamed to Open Image Tracer; native title bar; colored tracing progress; MSI installer
* Wed Nov 04 2026 Sabaoon <sabaoongfx@gmail.com> - 0.1.1-1
- Native packages, AppImage, Windows and macOS builds
