"""Turn an open CAJ document into a PDF.

Three output modes, all driven from the same two library calls:

``text``    selectable text only. Every layout run from ``CAJFILE_GetPageInfo``
            becomes a kerned ``TJ`` block, so characters land on the per-glyph x
            the CAJ file records (plus the calibrated pen offset below). Small
            files, real text extraction.
``image``   the library's own raster (``CAJFILE_DrawPageSlice1``) at a chosen
            dpi, as a single image XObject per page. Pixel-faithful to what
            cajviewer shows, no text layer.
``hybrid``  the raster underneath plus a text layer in render mode 3 (invisible).
            Faithful *and* selectable; the largest file.

The text layer embeds a TrueType subset of whatever font the library actually
rasterizes CJK with (see ``cajlib.DEFAULT_BASE_FONT``), because the 53 files
``fontmap.xml`` asks for are not in the shipped tree. That makes the PDF's glyphs
match the raster we can produce, which is the only self-consistent choice here.
"""
import os
import sys

from . import _img
from . import _pdf
from . import cajlib

# ---------------------------------------------------------------------------
# Text geometry, calibrated against the library's own raster (see below).
#
# CAJFILE_GetPageInfo reports, per run, an em size and a box in CAJ units
# (1/100 pt) from the page's top-left, plus a per-character x. Measured on the
# test document at 600 and 1200 dpi by cross-correlating glyphs rendered from
# the substitute font's exact glyf bboxes against the library's raster
# (16 isolated runs, median NCC 0.97):
#
#   baseline  == y_bottom                     (offset -0.075 pt +/- 0.016)
#   pen_x[i]  == x[i]/100 + PEN_X_OFFSET_PT   (+1.87 pt, sd 0.011, constant
#                                              in pt from em 8.71 to 27.15)
#   em        == size, NOT y_bottom - y_top   (they differ on 21/1312 runs;
#                                              the raster matches `size`)
#
# The x offset is a property of how the library positions glyphs relative to
# what GetPageInfo reports (likely the original document font's side bearing);
# it is empirical but dpi-invariant and size-invariant on every run measured.
# ---------------------------------------------------------------------------
PEN_X_OFFSET_PT = 1.87

__all__ = [
    "ConvertError", "Options", "FontManager", "convert", "convert_file",
    "PEN_X_OFFSET_PT",
]


class ConvertError(RuntimeError):
    pass


class Options:
    """Everything the converter can be told to do, in one place."""

    __slots__ = (
        "mode", "dpi", "image_mode", "image_bits", "jpeg_quality",
        "zlib_level", "pen_x_offset", "subset", "title", "author",
        "producer", "creator", "base_font", "font_dir", "family_map",
        "fallback_font", "text_color", "verbose",
    )

    def __init__(self, mode="text", dpi=200, image_mode="auto", image_bits=0,
                 jpeg_quality=85, zlib_level=9, pen_x_offset=PEN_X_OFFSET_PT,
                 subset=True, title=None, author=None, producer="caj2pdf",
                 creator="caj2pdf", base_font=None, font_dir=None,
                 family_map=None, fallback_font=None, text_color=None,
                 verbose=False):
        self.mode = mode
        self.dpi = int(dpi)
        self.image_mode = image_mode
        self.image_bits = int(image_bits)
        self.jpeg_quality = int(jpeg_quality)
        self.zlib_level = int(zlib_level)
        self.pen_x_offset = float(pen_x_offset)
        self.subset = bool(subset)
        self.title = title
        self.author = author
        self.producer = producer
        self.creator = creator
        self.base_font = base_font or cajlib.DEFAULT_BASE_FONT
        self.font_dir = font_dir
        self.family_map = dict(family_map or {})
        self.fallback_font = fallback_font
        self.text_color = text_color
        self.verbose = bool(verbose)

    def log(self, msg):
        if self.verbose:
            sys.stderr.write("[caj2pdf] %s\n" % msg)


class EmbeddedFont:
    """One substitute face, subset to the characters the document uses."""

    __slots__ = ("family", "path", "sub", "key", "font", "missing")

    def __init__(self, family, path, sub, key):
        self.family = family
        self.path = path
        self.sub = sub
        # The resource key is fixed per face for the whole document, so the same
        # font object can be referenced from every page's /Font dict.
        self.key = key
        self.font = None          # the _pdf.Font, created on first page
        self.missing = set()

    @property
    def code_to_gid(self):
        return self.sub["code_to_gid"]

    @property
    def metrics(self):
        return self.sub["metrics"]

    @property
    def upem(self):
        return int(self.sub["upem"])

    def gids(self, text):
        c2g = self.sub["code_to_gid"]
        out = []
        for ch in text:
            g = c2g.get(ord(ch))
            if g is None:
                self.missing.add(ch)
                g = 0
            out.append(g)
        return out


class FontManager:
    """Resolve CAJ family names to embedded substitute fonts.

    The shipped ``fontmap.xml`` names 53 font files that are not in the package,
    so almost every CJK family the document asks for is unresolvable. We map each
    family to a real file on disk -- by default the same face the library falls
    back to -- and embed a subset of it once per document.
    """

    def __init__(self, opts, codepoints_by_family, all_codepoints):
        self.opts = opts
        self.font_dir = opts.font_dir or self._default_font_dir()
        self.family_map, self.fallback = self._resolve_map(
            opts, codepoints_by_family, all_codepoints, self.font_dir)
        self._cache = {}
        self._by_path = {}       # resolved path -> EmbeddedFont (shared face)
        # Stable resource keys: family order is sorted so output is reproducible.
        self._keys = {fam: "F%d" % (i + 1)
                      for i, fam in enumerate(sorted(codepoints_by_family))}

    # -- construction -----------------------------------------------------
    @staticmethod
    def _default_font_dir():
        """The cajfonts directory next to the resource base we loaded from.

        Prefer the base the library was actually initialised with (recorded by
        cajlib.load) so an explicit --resdir/--lib is honoured; only fall back to
        a fresh argument-less find_library() search if nothing was loaded yet.
        """
        base = cajlib.resolved_base()
        if not base:
            try:
                _lib, base = cajlib.find_library()
            except cajlib.CAJError:
                return None
        cand = os.path.join(base, "Resource", "cajfonts")
        return cand if os.path.isdir(cand) else None

    @staticmethod
    def _resolve_map(opts, codepoints_by_family, all_codepoints, font_dir=None):
        """family -> filename, plus the fallback filename."""
        mapping = dict(opts.family_map)
        fallback = opts.fallback_font
        if not fallback:
            try:
                from . import fonts as _fonts
            except ImportError:
                _fonts = None
            if _fonts is not None:
                fallback = getattr(_fonts, "FALLBACK", None)
                if not mapping:
                    mapping = dict(getattr(_fonts, "FAMILY_MAP", {}))
        if not fallback:
            # Last resort: the base font's own file, found by its internal
            # name. Search the effective font dir (--fontdir wins), falling
            # back to the viewer's own cajfonts tree.
            fallback = FontManager._find_by_family(font_dir, opts.base_font)
            if not fallback:
                fallback = FontManager._find_by_family(
                    FontManager._default_font_dir(), opts.base_font)
        if not fallback:
            raise ConvertError(
                "no substitute font available; pass --fontdir DIR or "
                "--fallback FILE")
        for fam in codepoints_by_family:
            mapping.setdefault(fam, fallback)
        return mapping, fallback

    @staticmethod
    def _find_by_family(font_dir, family):
        """Locate a file whose name table advertises `family`."""
        if not font_dir or not os.path.isdir(font_dir):
            return None
        try:
            from . import ttfsub
        except ImportError:
            ttfsub = None
        want = family.strip().lower().replace(" ", "")
        for fn in sorted(os.listdir(font_dir)):
            if not fn.lower().endswith((".ttf", ".otf")):
                continue
            path = os.path.join(font_dir, fn)
            names = set()
            if ttfsub is not None:
                try:
                    names = {n.strip().lower().replace(" ", "")
                             for n in ttfsub.font_names(open(path, "rb").read())}
                except Exception:
                    names = set()
            if not names:
                names = {os.path.splitext(fn)[0].lower()}
            if want in names:
                return fn
        return None

    # -- public -----------------------------------------------------------
    def families(self):
        return sorted(self._keys)

    def get(self, family, codepoints):
        """Return the EmbeddedFont for `family`, building it on first use."""
        f = self._cache.get(family)
        if f is not None:
            return f
        fn = self.family_map.get(family) or self.fallback
        path = fn if os.path.isabs(fn) else os.path.join(
            self.font_dir or "", fn)
        if not path or not os.path.isfile(path):
            raise ConvertError(
                "substitute font for family %r not found: %r" % (family, path))
        path = os.path.realpath(path)
        # Several CAJ families routinely resolve to the same substitute face
        # (every unresolvable 方正* name lands on the base font). Embed that face
        # ONCE and let the families share the object, otherwise the file grows
        # by a whole font program per family.
        shared = self._by_path.get(path)
        if shared is not None:
            self._cache[family] = shared
            return shared
        data = open(path, "rb").read()
        if self.opts.subset:
            from . import ttfsub
            sub = ttfsub.subset(data, codepoints)
        else:
            sub = embed_whole_font(data, codepoints)
        key = self._keys.setdefault(family, "F%d" % (len(self._keys) + 1))
        self.opts.log("font %r -> %s (%d glyphs, %d bytes)"
                      % (family, os.path.basename(path), sub["num_glyphs"],
                         len(sub["bytes"])))
        f = EmbeddedFont(self._pdf_name_for(data, family), path, sub, key)
        self._cache[family] = f
        self._by_path[path] = f
        return f

    @staticmethod
    def _pdf_name_for(data, family):
        """Name the embedded font after what it actually IS.

        When one substitute serves several CAJ families the family name is
        arbitrary, so prefer the substitute's own PostScript/family name from its
        name table -- that is what a PDF reader reports, and it keeps the
        BaseFont honest instead of claiming to be 方正书宋_GBK.
        """
        try:
            from . import ttfsub
            ps = ttfsub.postscript_name(data) or ttfsub.family_name(data)
        except Exception:
            ps = None
        if ps:
            ps = "".join(ch for ch in ps if ch.isascii() and (
                ch.isalnum() or ch in "-_ "))
            ps = ps.strip().replace(" ", "")
            if ps:
                return ps
        return family

    def register_on(self, page, ef):
        """Make `ef` usable on `page`.

        The font *program* object is created once (on the first page that needs
        it); later pages just reference the same object number under the same
        resource key, which is what keeps a 4-page document from embedding four
        copies of the subset.
        """
        if ef.font is None:
            ef.font = page.add_truetype_font(
                ef.family, ef.sub["bytes"], ef.sub["code_to_gid"],
                ef.sub["gid_to_adv"], ef.upem, ef.metrics,
                resource_key=ef.key)
            ref = ef.font.ref
        else:
            ref = ef.font.ref
            page.add_resource("Font", ef.key, ref)
            if "/Text" not in page.procset:
                page.procset.append("/Text")
        return ef.font


def embed_whole_font(data, codepoints):
    """No-subsetting path: embed the whole face. Only useful for debugging."""
    from . import ttfsub
    c2g = ttfsub.build_cmap(data)
    adv, _n = _pdf.ttf_advance_table(data)
    metrics = _pdf.parse_ttf_metrics(data)
    upem = int(metrics.get("upem") or 2048)
    sel = {}
    missing = []
    for cp in codepoints:
        g = c2g.get(cp)
        if not g:
            missing.append(cp)
        else:
            sel[cp] = g
    gid_to_adv = {g: int(adv[g]) if g < len(adv) else 0 for g in sel.values()}
    gid_to_adv.setdefault(0, int(adv[0]) if adv else 0)
    return {
        "bytes": data, "code_to_gid": sel, "gid_to_adv": gid_to_adv,
        "upem": upem, "metrics": metrics, "num_glyphs": len(adv),
        "missing": sorted(missing),
    }


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------
def baseline_pt(run, page_h_pt):
    """PDF baseline y for a run: CAJ's ``y_bottom`` IS the baseline.

    Calibrated at 600/1200 dpi against the library raster: the measured offset
    from y_bottom is -0.075 pt (+/- 0.016 sd over 16 runs) -- well under a
    pixel at any sane rendering resolution -- so it is exactly zero and the
    tiny residual is rasterization noise.
    """
    return page_h_pt - run.y_bottom / cajlib.UNITS_PER_PT


def pen_x_pt(run, i, offset_pt=PEN_X_OFFSET_PT):
    """Pen x in pt for character `i` of `run` (calibrated constant offset).

    ``run.x`` normally carries exactly one entry per character. If it is ever
    short, extend the run with its own mean advance rather than dropping the
    tail -- the text still lands on the same line at a plausible position. An
    entirely empty ``run.x`` has no anchor to extrapolate from, so it returns
    None (the caller skips the run).
    """
    xs = run.x
    if not xs:
        return None
    if i < len(xs):
        return xs[i] / cajlib.UNITS_PER_PT + offset_pt
    step = ((xs[-1] - xs[0]) / (len(xs) - 1)) if len(xs) > 1 else run.size
    return (xs[-1] + step * (i - len(xs) + 1)) / cajlib.UNITS_PER_PT + offset_pt


def run_size_pt(run):
    """The em size to draw at, in points: CAJ's float ``size`` field.

    For 1291/1312 runs of the test document ``y_bottom - y_top == size``; on
    the 21 exceptions the raster still matches ``size``, not the box height,
    so ``size`` is the em the library renders with.
    """
    return run.size / cajlib.UNITS_PER_PT


# --------------------------------------------------------------------------
# conversion
# --------------------------------------------------------------------------
def collect_text(doc, pages):
    """Gather runs plus the character set each family needs."""
    runs_by_page = {}
    cp_by_family = {}
    all_cp = set()
    for p in pages:
        runs = doc.page_text_runs(p)
        runs_by_page[p] = runs
        for r in runs:
            cps = {ord(c) for c in r.text}
            cp_by_family.setdefault(r.font, set()).update(cps)
            all_cp |= cps
    return runs_by_page, cp_by_family, all_cp


def _add_raster(page, doc, pdf, p, opts, wpt, hpt):
    """Render page `p` and place it as a full-page image XObject.

    The image dictionary is assembled directly from the encoder's output rather
    than through ``Page.add_image`` so that low-bit-depth modes (bilevel, 4-bit)
    -- which the convenience wrapper does not model -- also work.
    """
    w, h, _depth, stride, data = doc.render(p, dpi=opts.dpi)
    enc = _img.encode_image_for_pdf(
        data, w, h, mode=opts.image_mode, quality=opts.jpeg_quality,
        raw_mode="BGR", stride=stride, zlib_level=opts.zlib_level,
        bits=opts.image_bits or (4 if opts.image_mode == "lowbits" else 1))

    entries = {
        "Type": "/XObject", "Subtype": "/Image",
        "Width": int(enc["width"]), "Height": int(enc["height"]),
        "ColorSpace": "/" + enc["colorspace"],
        "BitsPerComponent": int(enc["bits"]),
    }
    stream = _pdf.PDFStream(
        enc["data"], entries=entries, compress=False, pre_compressed=True,
        filter_name=enc.get("filter_name"),
        decode_parms=enc.get("decode_parms"))
    ref = pdf.make_indirect(stream)
    key = "Im%d" % p
    page.add_resource("XObject", key, ref)
    if "/ImageC" not in page.procset:
        page.procset.append("/ImageC")
    page.place_xobject(key, 0.0, 0.0, wpt, hpt)
    opts.log("page %d raster %dx%d %s/%s -> %d bytes"
             % (p, w, h, enc.get("colorspace"), enc.get("bits"),
                len(enc["data"])))


def convert(doc, opts=None, pages=None, out=None):
    """Convert an open CAJDoc to a PDF. Returns the PDF bytes."""
    opts = opts or Options()
    n = doc.page_count
    if pages is None:
        pages = list(range(1, n + 1))
    # Order-preserving dedupe: a repeated page number would otherwise be
    # emitted (and, in image modes, rendered) twice.
    pages = list(dict.fromkeys(p for p in pages if 1 <= p <= n))
    if not pages:
        raise ConvertError("no pages to convert")

    want_text = opts.mode in ("text", "hybrid")
    want_image = opts.mode in ("image", "hybrid")

    runs_by_page, cp_by_family, all_cp = ({}, {}, set())
    fm = None
    if want_text:
        runs_by_page, cp_by_family, all_cp = collect_text(doc, pages)
        fm = FontManager(opts, cp_by_family, all_cp)
        opts.log("%d families, %d distinct codepoints"
                 % (len(cp_by_family), len(all_cp)))

    pdf = _pdf.PDFDocument(
        title=opts.title, author=opts.author, producer=opts.producer,
        creator=opts.creator, version="1.7",
        compress_level=opts.zlib_level)

    for p in pages:
        uw, uh = doc.page_size_units(p)
        wpt = uw / cajlib.UNITS_PER_PT
        hpt = uh / cajlib.UNITS_PER_PT
        page = pdf.add_page(wpt, hpt)

        if want_image:
            _add_raster(page, doc, pdf, p, opts, wpt, hpt)

        if not want_text:
            continue

        runs = runs_by_page.get(p) or []
        if not runs:
            opts.log("page %d: no text runs" % p)
            continue

        # Register only the families this page uses, so the Resources dict
        # stays small. Codepoints are document-wide: a subset built per page
        # would balloon the file with near-duplicate font programs.
        page_fonts = {}
        for fam in sorted({r.font for r in runs}):
            ef = fm.get(fam, all_cp)
            page_fonts[fam] = (fm.register_on(page, ef), ef)

        if opts.mode == "hybrid":
            # Text rendering mode 3 lays glyphs out (so they are selectable and
            # searchable) without painting them, letting the raster show through.
            # It is graphics state, so it persists across the BT..ET that
            # show_run emits for every run below.
            page.raw(b"BT 3 Tr ET\n")
        if opts.text_color:
            r, g, b = opts.text_color
            page.set_fill_rgb(r, g, b)

        emitted = 0
        for run in runs:
            entry = page_fonts.get(run.font)
            if entry is None:
                continue
            font, ef = entry
            if not run.x:
                opts.log("page %d: skipping run with text but no x positions: %r"
                         % (p, run.text[:24]))
                continue
            gids = ef.gids(run.text)
            if not gids:
                continue
            size = run_size_pt(run)
            xs = [pen_x_pt(run, i, opts.pen_x_offset) for i in range(len(gids))]
            xs = [x for x in xs if x is not None]
            if not xs:
                continue
            y = baseline_pt(run, hpt)
            page.show_run(font, gids, xs, size, y)
            emitted += 1
        opts.log("page %d: %d runs, %d chars"
                 % (p, emitted, sum(r.char_count for r in runs)))

    if fm:
        for fam, ef in fm._cache.items():
            if ef.missing:
                opts.log("font %r: %d unmapped chars %r"
                         % (fam, len(ef.missing), sorted(ef.missing)[:8]))

    data = pdf.serialize()
    if out:
        with open(out, "wb") as fh:
            fh.write(data)
    return data


def convert_file(caj_path, pdf_path, opts=None, lib_path=None, res_base=None):
    """Open `caj_path`, convert, write `pdf_path`. Returns the byte count."""
    opts = opts or Options()
    lib, _base = cajlib.load(lib_path, res_base, base_font=opts.base_font)
    doc = cajlib.open_doc(caj_path, lib=lib)
    try:
        if not opts.title:
            opts.title = os.path.splitext(os.path.basename(caj_path))[0]
        data = convert(doc, opts)
    finally:
        doc.close()
    with open(pdf_path, "wb") as fh:
        fh.write(data)
    return len(data)
