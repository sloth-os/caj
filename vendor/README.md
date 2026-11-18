# vendor/

Third-party runtime needed by caj2pdf, vendored so CI (and any clone) is
self-contained. Nothing here is authored by this project.

| file | sha256 | size |
| --- | --- | --- |
| `cajviewer-1.0-1.el7.noarch.rpm` | `f3730fad4d45873474c84dfbc8b2228a8b2a2e1ef0321e44005337190f88b5f3` | 35,481,812 B |

**What it provides** (extracted with `rpm2cpio | cpio` into `/`):

- `opt/cajviewer/usr/lib/libreaderex_x64.so` — the CAJ rendering engine
  (14.7 MB) whose `CAJFILE_*` C API this project drives via ctypes.
- `opt/cajviewer/usr/bin/Resource/cajfonts/` — font tree; only
  `wqy-microhei_0.ttf` (WenQuanYi Micro Hei, GPLv3 with font exception) is
  actually used, as the `CAJFILE_SetBaseFont` fallback and the text-layer
  substitute.

**Origin**: the CNKI (Tsinghua Tongfang) CAJViewer Linux distribution, the
same RPM that was analysed during development. Redistribution is the
vendor's, not this project's, decision — this repository keeps it because
the converter is non-functional without it, and the GitHub Actions e2e
test renders a real `.caj` through it. Remove this directory and repoint
`CAJREADER_LIB`/`CAJVIEWER_DIR` at your own copy if you prefer.

Keep this file's sha256 table in sync with the contents (CI does not check
it, but reviewers should).
