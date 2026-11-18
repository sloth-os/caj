# caj2pdf

Convert CNKI `.caj` files to PDF by driving the shipped CAJ viewer library
(`libreaderex_x64.so`) directly from Python — no GUI, no X server, no other
CNKI software. Pure stdlib plus `numpy` and `Pillow`; there is no fontTools,
reportlab or poppler here, so the TrueType subsetter and the PDF writer are
hand-rolled.

## Usage

```sh
./caj2pdf.py document.caj                       # -> document.pdf, selectable text
./caj2pdf.py document.caj -o out.pdf
./caj2pdf.py document.caj --mode image --dpi 300
./caj2pdf.py document.caj --mode hybrid --dpi 200
./caj2pdf.py *.caj --outdir out/
./caj2pdf.py document.caj --pages 1,3-5 -v
```

| mode | what lands in the PDF |
| --- | --- |
| `text` (default) | selectable text only — smallest, real copy/paste and search |
| `image` | the library's own raster per page — pixel-faithful, no text layer |
| `hybrid` | the raster plus an invisible (`3 Tr`) selectable text layer on top |

Useful switches: `--dpi`, `--image-mode {auto,flate,dct,bilevel,lowbits,...}`,
`--image-bits`, `--jpeg-quality`, `--no-subset`, `--pen-x-offset`, `--lib`,
`--resdir`, `--fontdir`, `--base-font`, `--fallback`, `--title/--author/
--producer`, `--pages`, `-v`, `--debug-log`.

## Layout

```
caj2pdf.py           CLI
cajpdf/cajlib.py     ctypes binding for libreaderex_x64.so (the source of truth)
cajpdf/convert.py    CAJ runs -> PDF pages: geometry, fonts, the three modes
cajpdf/ttfsub.py     TrueType helpers: sfnt directory, cmap, name, subsetting
cajpdf/_ttfsub_impl.py  the subsetter itself
cajpdf/_pdf.py       PDF 1.7 writer: pages, images, Type0/CIDFontType2 fonts
cajpdf/_img.py       page bitmap -> image XObject (flate/PNG-predictor/DCT/bilevel)
```

As a library:

```python
from cajpdf import cajlib
from cajpdf.convert import Options, convert_file

convert_file("in.caj", "out.pdf", Options(mode="text"))
```

## What had to be reverse-engineered

Three facts are load-bearing; each is documented at the top of the relevant
module.

1. **`CAJFILE_Init(dir, dir, 0, dir)` takes the directory containing
   `Resource/`**, not the path to the `cajviewer` executable. Passing the exe
   makes the library look for fonts in the wrong place and every font lookup
   fails.
2. **`CAJFILE_SetBaseFont(b"WenQuanYi Micro Hei")` is mandatory**, called after
   `Init` and before `Open`. All 53 font files the shipped `fontmap.xml` names
   are absent from the package, so without a fallback family the library
   resolves no CJK face and silently skips the glyph ink — you get two
   horizontal rules and a blank page. With it, page 1 of the test document goes
   from 14k to 172k dark pixels at 150 dpi. The call returns void, is
   process-global and latched by its first successful call, and an
   unrecognised name is silently ignored.
3. **`CAJFILE_DrawPageSlice1` is the only synchronous render path.** Its
   `tagLOGPAGE` argument is 11 × int32 (idx0 = 0x2c, idx3 = percent scale,
   idx6/idx7 = canvas size, idx10 = dpi) and all 8 trailing out-pointers must
   be non-NULL.

Text does not come from `CAJFILE_GetPageText` (it returns nothing on this
build) but from `CAJFILE_GetPageInfo`, whose runs carry real Unicode UTF-16LE
text, a per-character x, a y_top/y_bottom box, a float em size and a font name.

CAJ units are 1/100 of a PostScript point; `px = units * dpi / 7200`.

## Text geometry

The mapping from CAJ's reported run geometry to PDF placement was calibrated
against the library's own raster: glyphs predicted from the substitute font's
exact `glyf` bounding boxes were cross-correlated sub-pixel against the
rendered page (16 isolated runs at 600 and 1200 dpi, median normalised
correlation 0.97). The result, encoded in `cajpdf/convert.py`:

- the run's `y_bottom` **is** the glyph baseline (measured residual
  −0.075 pt ± 0.016);
- each pen sits a constant **+1.87 pt** right of the reported `x` (sd 0.011 pt,
  constant in points from 8.71 pt to 27.15 pt em sizes);
- the em size is the run's float `size` field, not `y_bottom - y_top`.

With this the text layer and the library's raster agree to a per-run column IoU
of 0.98 and row IoU of 1.000; page-wide ink IoU is 0.67, and the remaining
difference is glyph *shape* (the substitute face is not the original 方正书宋_GBK,
which is not shipped), not placement. Since every glyph is placed at its own
absolute x, CAJ's justified spacing survives even though the substitute font's
own advances differ.

## Notes

- `Resource/cajfonts/` entries are symlinks; writing through one overwrites the
  file it points at. Remove the link before replacing a font.
- A successful `CAJFILE_Init` prints `string to int` about 24 times. Benign.
