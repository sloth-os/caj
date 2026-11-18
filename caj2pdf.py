#!/usr/bin/env python3
"""caj2pdf -- convert CAJ (Tsinghua Tongfang) files to PDF.

Yes, this is possible by driving the shipped viewer library directly: cajviewer's
`libreaderex_x64.so` exports a `CAJFILE_*` C API that ctypes can call with no
GUI, no X server and no other CNKI software.

    ./caj2pdf.py document.caj                     -> document.pdf (selectable text)
    ./caj2pdf.py document.caj -o out.pdf
    ./caj2pdf.py document.caj --mode image --dpi 300
    ./caj2pdf.py document.caj --mode hybrid --dpi 200
    ./caj2pdf.py *.caj --outdir out/

Three things had to be reverse-engineered to make it work, and all three are
load-bearing:

1. `CAJFILE_Init(dir, dir, 0, dir)` takes the *directory containing Resource/*,
   not the path to the cajviewer executable. Passing the exe makes the library
   look for `/usr/bin/cajviewer/Resource/cajfonts`, so every font lookup fails.
2. `CAJFILE_SetBaseFont("WenQuanYi Micro Hei")` is mandatory. All 53 font files
   that the shipped `fontmap.xml` names are missing from the package, so without
   a fallback family the library resolves no CJK face at all and silently skips
   the glyph ink -- you get two horizontal rules and a blank page. With it, page
   1 of the test document goes from 48k to 643k dark pixels at 300 dpi.
3. `CAJFILE_DrawPageSlice1` is the only synchronous render path. Its `tagLOGPAGE`
   argument is 11 x int32: idx0 = 0x2c, idx3 = percent scale, idx6/idx7 = canvas
   size (used to allocate the output buffer and set stride), idx10 = dpi, and all
   8 trailing out-pointers must be non-NULL.

Text comes from `CAJFILE_GetPageInfo`, not `CAJFILE_GetPageText` (which returns
nothing on this build): the PageInfo array carries real Unicode UTF-16LE text
with a per-character x, a y_top/y_bottom box, a float em size and a font name
for every layout run. Calibrated against the library's own raster (sub-pixel
cross-correlation, 16 isolated runs, NCC ~0.97): y_bottom IS the glyph
baseline, the em size is the run's float `size` field, and the library places
each pen a constant +1.87 pt right of the reported x (dpi- and size-invariant).

CAJ units are 1/100 of a PostScript point, and pixels = units * dpi / 7200.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cajpdf import cajlib                      # noqa: E402
from cajpdf.convert import (                   # noqa: E402
    PEN_X_OFFSET_PT, ConvertError, Options, convert,
)

PROG = "caj2pdf.py"


def build_parser():
    ap = argparse.ArgumentParser(
        prog=PROG, description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="modes:\n"
               "  text    selectable text only (default, smallest)\n"
               "  image   the library's raster only (pixel-faithful, no text layer)\n"
               "  hybrid  raster underneath + invisible selectable text on top\n")
    ap.add_argument("inputs", nargs="+", metavar="INPUT.caj",
                    help="one or more .caj files")
    ap.add_argument("-o", "--output", metavar="FILE",
                    help="output PDF (only valid with a single input)")
    ap.add_argument("--outdir", metavar="DIR",
                    help="write each PDF next to nothing: into DIR instead")
    ap.add_argument("-m", "--mode", choices=("text", "image", "hybrid"),
                    default="text", help="what to put in the PDF")
    ap.add_argument("--dpi", type=int, default=200,
                    help="raster resolution for image/hybrid modes (default 200)")
    ap.add_argument("--image-mode", default="auto",
                    choices=("auto", "flate", "flate_gray", "flate_rgb", "dct",
                             "dct_gray", "dct_rgb", "bilevel", "lowbits"),
                    help="how to encode the page bitmap (default auto)")
    ap.add_argument("--image-bits", type=int, default=0, metavar="N",
                    choices=(0, 1, 2, 4),
                    help="bit depth for --image-mode lowbits (1/2/4; default "
                         "4). 0 means: pick the mode's default (lowbits=4, "
                         "bilevel=1)")
    ap.add_argument("--jpeg-quality", type=int, default=85,
                    help="quality for the dct image modes (default 85)")
    ap.add_argument("--zlib-level", type=int, default=9,
                    help="deflate level for streams (default 9)")
    ap.add_argument("--no-subset", action="store_true",
                    help="embed whole font files instead of subsets (much bigger)")
    ap.add_argument("--pen-x-offset", type=float, default=PEN_X_OFFSET_PT,
                    metavar="PT",
                    help="constant added to every glyph's reported x "
                         "(default %(default)s pt, calibrated against the "
                         "library's own raster)")
    ap.add_argument("--lib", metavar="PATH", dest="lib_path",
                    help="path to %s (default: search)" % cajlib.LIBNAME)
    ap.add_argument("--resdir", metavar="DIR", dest="res_base",
                    help="directory containing Resource/cajfonts (default: search)")
    ap.add_argument("--fontdir", metavar="DIR",
                    help="directory of substitute TTFs (default: Resource/cajfonts)")
    ap.add_argument("--base-font", metavar="NAME",
                    default=cajlib.DEFAULT_BASE_FONT,
                    help="fallback family handed to CAJFILE_SetBaseFont "
                         "(default %(default)r)")
    ap.add_argument("--fallback", metavar="FILE", dest="fallback_font",
                    help="font file used when a family has no explicit substitute")
    ap.add_argument("--title", help="PDF /Title")
    ap.add_argument("--author", help="PDF /Author")
    ap.add_argument("--producer", default="caj2pdf", help="PDF /Producer")
    ap.add_argument("--pages", metavar="LIST",
                    help="1-based page list, e.g. 1,3-5 (default: all)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log progress to stderr")
    ap.add_argument("--debug-log", metavar="PATH",
                    help="have the library write its own diagnostics here")
    return ap


def parse_pages(spec):
    """'1,3-5' -> [1, 3, 4, 5]. None -> None (all pages)."""
    if not spec:
        return None
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    if not out:
        raise ValueError("empty page list %r" % (spec,))
    return sorted(set(out))


def out_path_for(src, args, taken=None):
    """Destination PDF path for one input.

    With ``--outdir`` two inputs may share a stem (``a/foo.caj``, ``b/foo.CAJ``);
    without uniquing the second would silently clobber the first while both
    were reported as successes, so a counter suffix is appended on collision.
    """
    stem = os.path.splitext(os.path.basename(src))[0]
    if args.output:
        return args.output
    if args.outdir:
        dst = os.path.join(args.outdir, stem + ".pdf")
        i = 2
        while taken is not None and dst in taken:
            dst = os.path.join(args.outdir, "%s-%d.pdf" % (stem, i))
            i += 1
        return dst
    return os.path.join(os.path.dirname(os.path.abspath(src)), stem + ".pdf")


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.output and len(args.inputs) > 1:
        sys.exit("%s: -o/--output takes a single input file" % PROG)
    try:
        pages = parse_pages(args.pages)
    except ValueError as e:
        sys.exit("%s: %s" % (PROG, e))
    if args.outdir:
        # Created once, up front: a bad --outdir (exists as a file, unwritable,
        # ...) is a configuration error for the whole run, not a per-file one.
        try:
            os.makedirs(args.outdir, exist_ok=True)
        except OSError as e:
            sys.exit("%s: cannot create --outdir %s: %s" % (PROG, args.outdir, e))

    opts = Options(
        mode=args.mode, dpi=args.dpi, image_mode=args.image_mode,
        image_bits=args.image_bits,
        jpeg_quality=args.jpeg_quality, zlib_level=args.zlib_level,
        pen_x_offset=args.pen_x_offset, subset=not args.no_subset,
        title=args.title, author=args.author, producer=args.producer,
        creator=PROG, base_font=args.base_font, font_dir=args.fontdir,
        fallback_font=args.fallback_font,
        verbose=args.verbose)

    try:
        lib, base = cajlib.load(args.lib_path, args.res_base,
                                base_font=opts.base_font,
                                debug_log=args.debug_log)
    except (cajlib.CAJError, OSError) as e:
        sys.exit("%s: cannot initialize the CAJ library: %s" % (PROG, e))
    if args.verbose:
        sys.stderr.write("[caj2pdf] library resources: %s\n" % base)

    rc = 0
    taken = set()
    for src in args.inputs:
        if not os.path.isfile(src):
            sys.stderr.write("%s: no such file: %s\n" % (PROG, src))
            rc = 1
            continue
        dst = out_path_for(src, args, taken)
        taken.add(dst)
        stem = os.path.splitext(os.path.basename(src))[0]
        per_file = Options(
            mode=opts.mode, dpi=opts.dpi, image_mode=opts.image_mode,
            image_bits=opts.image_bits,
            jpeg_quality=opts.jpeg_quality, zlib_level=opts.zlib_level,
            pen_x_offset=opts.pen_x_offset, subset=opts.subset,
            title=opts.title or stem, author=opts.author,
            producer=opts.producer, creator=opts.creator,
            base_font=opts.base_font, font_dir=opts.font_dir,
            family_map=opts.family_map, fallback_font=opts.fallback_font,
            text_color=opts.text_color, verbose=opts.verbose)
        try:
            doc = cajlib.open_doc(src, lib=lib)
        except cajlib.CAJError as e:
            sys.stderr.write("%s: %s: %s\n" % (PROG, src, e))
            rc = 1
            continue
        try:
            n = doc.page_count
            sel = pages or list(range(1, n + 1))
            dropped = sorted(p for p in sel if not 1 <= p <= n)
            if dropped:
                sys.stderr.write(
                    "%s: %s: pages %s out of range (1-%d), skipped\n"
                    % (PROG, src, ",".join(map(str, dropped)), n))
            data = convert(doc, per_file, pages=sel)
        except (ConvertError, cajlib.CAJError, OSError) as e:
            sys.stderr.write("%s: %s: %s\n" % (PROG, src, e))
            rc = 1
            continue
        except Exception as e:                      # noqa: BLE001 - keep going
            # A malformed or hostile input must not abort the rest of the batch.
            sys.stderr.write("%s: %s: %s: %s\n" % (PROG, src,
                                                   type(e).__name__, e))
            rc = 1
            continue
        finally:
            doc.close()
        try:
            os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".",
                        exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(data)
        except OSError as e:
            sys.stderr.write("%s: %s: %s\n" % (PROG, dst, e))
            rc = 1
            continue
        n_out = len([p for p in sel if 1 <= p <= n])
        print("%s -> %s (%d pages, %d bytes)"
              % (src, dst, n_out, len(data)))
    return rc


if __name__ == "__main__":
    sys.exit(main())
