"""ctypes binding for libreaderex_x64.so's CAJFILE_* C API.

Everything here was reverse-engineered from the shipped .so (objdump/nm/gdb) and
verified against a real 4-page CAJ document. Page indices handed to the C API are
1-based; they are 0-based internally.

The single most important gotcha: CAJFILE_Init's first argument is the *directory*
that contains a `Resource/` subdirectory -- not the path to the cajviewer
executable. The library does `strcpy(base, arg1); pathAppend(base, "Resource");
pathAppend(base, "cajfonts")` unconditionally (GatherFont @0x47aa2a), so passing
the exe path makes it look for `/usr/bin/cajviewer/Resource/cajfonts`, every font
lookup fails, and pages render as two horizontal rules and nothing else.

The second gotcha: `CAJFILE_SetBaseFont` is mandatory. The shipped font tree is
missing every one of the 53 files that `fontmap.xml` points at, so without a base
font the library cannot resolve any CJK family and silently skips the glyph ink
(page 1 @300dpi: 48k dark px, title box empty). With a base font that is actually
registered -- `WenQuanYi Micro Hei`, the internal name of the shipped
`wqy-microhei_0.ttf` -- every lookup falls back to it and the same page renders
642k dark px with a fully inked title. The viewer calls it from `initReader`
(@0x412460 -> @0x412d9b). It returns an undefined value; never test it.
"""
import ctypes as C
import os
import struct

LIBNAME = "libreaderex_x64.so"

# Layout of the tagLOGPAGE struct: 11 x int32, size field = 0x2c.
LOGPAGE_SIZE = 0x2c
LOGPAGE_FIELDS = 11

# Default fallback family. Must be a name the library actually registered; an
# unknown name is silently ignored and leaves the render inkless.
DEFAULT_BASE_FONT = "WenQuanYi Micro Hei"


class CAJError(RuntimeError):
    pass


def _candidate_bases(explicit=None):
    """Yield directories that could contain a `Resource/` font tree."""
    seen = set()
    out = []

    def add(p):
        if not p:
            return
        p = os.path.abspath(os.path.expanduser(p))
        if p not in seen:
            seen.add(p)
            out.append(p)

    for p in (
        explicit,
        os.environ.get("CAJVIEWER_DIR"),
        "/opt/cajviewer/usr/bin",
        "/usr/bin",
        "/usr/local/bin",
    ):
        add(p)
    # Anything under the repo's extracted RPM tree.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for root, dirs, _files in os.walk(os.path.join(here, "tmp")):
        if "Resource" in dirs and root not in seen:
            seen.add(root)
            out.append(root)
        # Don't descend into Resource itself or .git.
        dirs[:] = [d for d in dirs if d not in ("Resource", ".git")]
    return out


def find_library(explicit_lib=None, explicit_base=None):
    """Locate (lib_path, resource_base). Raises CAJError if not found."""
    lib_candidates = [explicit_lib] if explicit_lib else []
    lib_candidates += [
        os.environ.get("CAJREADER_LIB"),
        "/opt/cajviewer/usr/lib/" + LIBNAME,
        "/usr/lib/" + LIBNAME,
        "/usr/lib64/" + LIBNAME,
    ]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for root, dirs, files in os.walk(os.path.join(here, "tmp")):
        if LIBNAME in files:
            lib_candidates.append(os.path.join(root, LIBNAME))
        dirs[:] = [d for d in dirs if d != ".git"]
    lib = next((p for p in lib_candidates if p and os.path.exists(p)), None)
    if lib is None:
        raise CAJError("could not find %s; pass --lib PATH" % LIBNAME)

    for base in _candidate_bases(explicit_base):
        if os.path.isdir(os.path.join(base, "Resource", "cajfonts")):
            return lib, base
    # Fall back to a base that at least has Resource/, so the caller can see why
    # fonts are missing rather than getting a silent blank render.
    for base in _candidate_bases(explicit_base):
        if os.path.isdir(os.path.join(base, "Resource")):
            return lib, base
    raise CAJError(
        "found %s but no directory containing Resource/cajfonts; pass --resdir DIR"
        % lib
    )


class CAJDoc:
    """A single open CAJ document."""

    def __init__(self, lib, handle):
        self._lib = lib
        self._h = handle
        self._closed = False

    # -- lifetime ---------------------------------------------------------
    def close(self):
        if not self._closed and self._h:
            self._lib.CAJFILE_Close(self._h)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @property
    def handle(self):
        return self._h

    # -- metadata ---------------------------------------------------------
    @property
    def page_count(self):
        return int(self._lib.CAJFILE_GetPageCount(self._h))

    def page_size_units(self, page1):
        """Return (w, h) in CAJ units (1/100 pt) for a 1-based page number."""
        wh = (C.c_int * 2)()
        self._prepare(page1)
        # The int return value is garbage on this build (-12752864); the
        # out-array is what carries the answer.
        self._lib.CAJFILE_GetPageSize(self._h, page1, wh, None)
        if wh[0] <= 0 or wh[1] <= 0:
            raise CAJError("page %d: bad size %dx%d" % (page1, wh[0], wh[1]))
        return int(wh[0]), int(wh[1])

    def is_image_page(self, page1):
        return bool(self._lib.CAJFILE_IsImagePage(self._h, page1))

    # -- raster -----------------------------------------------------------
    def render(self, page1, dpi=150, scale=100):
        """Render a page synchronously.

        Returns (width, height, depth, stride, raw_bytes) where raw_bytes is a
        BGR24 buffer of `stride * height` bytes.

        CAJFILE_DrawPageSlice1 is the only usable synchronous render path.
        CAJFILE_GetPageImage just looks up lru::ImageCache and returns NULL in a
        headless process, and CAJFILE_PreparePageImage merely queues an async job
        that nothing pumps.

        All 8 trailing out-pointers must be non-NULL: the wrapper writes logpage
        fields back through them and segfaults on NULL.
        """
        uw, uh = self.page_size_units(page1)
        # CAJ units are 1/100 of a PostScript point, so
        #   px = units * dpi / 7200        (45955 * 150 / 7200 = 957)
        w = max(1, int(round(uw * dpi / 7200.0 * scale / 100.0)))
        h = max(1, int(round(uh * dpi / 7200.0 * scale / 100.0)))

        log = (C.c_int * LOGPAGE_FIELDS)()
        log[0] = LOGPAGE_SIZE
        log[3] = scale          # percent scale, drives content size
        log[6] = w              # canvas size -- must be non-zero or it returns NULL
        log[7] = h
        log[10] = dpi

        out = [C.c_int(0) for _ in range(8)]
        buf = self._lib.CAJFILE_DrawPageSlice1(
            self._h, page1, log, *[C.byref(x) for x in out]
        )
        if not buf:
            raise CAJError("page %d: DrawPageSlice1 returned NULL" % page1)
        ow, oh, depth, stride = out[2].value, out[3].value, out[6].value, out[7].value
        if ow <= 0 or oh <= 0 or stride <= 0 or depth != 24:
            raise CAJError(
                "page %d: bad bitmap params %dx%d depth=%d stride=%d"
                % (page1, ow, oh, depth, stride)
            )
        # Keep a reference to the buffer alive while we copy it.
        data = C.string_at(buf, stride * oh)
        return ow, oh, depth, stride, data

    # -- structure --------------------------------------------------------
    def page_text_runs(self, page1):
        """Return a list of TextRun for a 1-based page number.

        CAJFILE_GetPageText returns 0/empty on this build, but CAJFILE_GetPageInfo
        exposes the real layout objects. The field offsets below were derived by
        disassembling DumpGetPageInfo (@0x5e4592), which prints these exact fields
        as XML attributes.
        """
        self._prepare(page1)
        pi = self._lib.CAJFILE_GetPageInfo(self._h, page1)
        if not pi:
            return []
        try:
            hdr = C.string_at(pi, 0x30)
            _w, _h, ntext = struct.unpack_from("<3i", hdr, 8)
            arr = struct.unpack_from("<Q", hdr, 0x14)[0]
            runs = []
            if ntext <= 0 or not arr:
                return runs
            ta = C.cast(arr, C.POINTER(C.c_uint64))
            for i in range(ntext):
                raw = C.string_at(ta[i], 0x60)
                name = raw[4:].split(b"\x00")[0].decode("utf-8", "replace")
                font_size = struct.unpack_from("<f", raw, 0x32)[0]
                char_count, y_top, y_bot = struct.unpack_from("<3i", raw, 0x42)
                if char_count <= 0 or char_count > 1 << 20:
                    continue
                x_ptr = struct.unpack_from("<Q", raw, 0x4E)[0]
                t_ptr = struct.unpack_from("<Q", raw, 0x56)[0]
                if not x_ptr or not t_ptr:
                    continue
                try:
                    text = C.string_at(t_ptr, char_count * 2).decode("utf-16-le", "replace")
                    xs = struct.unpack_from(
                        "<%di" % char_count, C.string_at(x_ptr, char_count * 4)
                    )
                except (OSError, struct.error, ValueError):
                    continue
                runs.append(
                    TextRun(
                        page=page1,
                        index=i,
                        font=name,
                        size=font_size,
                        char_count=char_count,
                        y_top=y_top,
                        y_bottom=y_bot,
                        x=list(xs),
                        text=text,
                    )
                )
            return runs
        finally:
            self._lib.CAJFILE_ReleasePageInfo(C.c_void_p(pi))

    # -- internals --------------------------------------------------------
    def _prepare(self, page1):
        # Returns 3 when ready; call before size/info/draw.
        self._lib.CAJFILE_PreparePage(self._h, page1, 0)


class TextRun:
    """One layout run: a span of characters sharing font and size.

    Positions are in CAJ units (1/100 pt) measured from the page's TOP-LEFT.
    """

    __slots__ = ("page", "index", "font", "size", "char_count", "y_top", "y_bottom", "x", "text")

    def __init__(self, page, index, font, size, char_count, y_top, y_bottom, x, text):
        self.page = page
        self.index = index
        self.font = font
        self.size = size
        self.char_count = char_count
        self.y_top = y_top
        self.y_bottom = y_bottom
        self.x = x
        self.text = text

    @property
    def chars(self):
        """Per-character (x_units, y_top_units, char) triples."""
        return list(zip(self.x, [self.y_top] * len(self.text), self.text))

    def __repr__(self):
        return "TextRun(p%d #%d %s %.0f y=%d x0=%d %r)" % (
            self.page, self.index, self.font, self.size, self.y_top,
            self.x[0] if self.x else -1, self.text[:24],
        )


def _bind(lib):
    i32 = C.c_int
    vp = C.c_void_p
    lib.CAJFILE_Init.restype = i32
    lib.CAJFILE_Init.argtypes = [C.c_char_p, C.c_char_p, i32, C.c_char_p]
    lib.CAJFILE_Open.restype = vp
    lib.CAJFILE_Open.argtypes = [C.c_char_p]
    lib.CAJFILE_Close.restype = i32
    lib.CAJFILE_Close.argtypes = [vp]
    lib.CAJFILE_GetPageCount.restype = i32
    lib.CAJFILE_GetPageCount.argtypes = [vp]
    lib.CAJFILE_PreparePage.restype = i32
    lib.CAJFILE_PreparePage.argtypes = [vp, i32, i32]
    lib.CAJFILE_GetPageSize.restype = i32
    lib.CAJFILE_GetPageSize.argtypes = [vp, i32, C.POINTER(i32), vp]
    lib.CAJFILE_IsImagePage.restype = i32
    lib.CAJFILE_IsImagePage.argtypes = [vp, i32]
    lib.CAJFILE_GetImageCount.restype = i32
    lib.CAJFILE_GetImageCount.argtypes = [vp, i32]
    lib.CAJFILE_DrawPageSlice1.restype = vp
    lib.CAJFILE_DrawPageSlice1.argtypes = [vp, i32, vp] + [vp] * 8
    lib.CAJFILE_GetPageInfo.restype = vp
    lib.CAJFILE_GetPageInfo.argtypes = [vp, i32]
    lib.CAJFILE_ReleasePageInfo.restype = None
    lib.CAJFILE_ReleasePageInfo.argtypes = [vp]
    # Returns void -- observed garbage (1854087584, -178052704, 463675808, ...)
    # depending on call site. restype=None so nobody mistakes it for a status.
    lib.CAJFILE_SetBaseFont.restype = None
    lib.CAJFILE_SetBaseFont.argtypes = [C.c_char_p]
    lib.CAJFILE_InitDebug.restype = None
    lib.CAJFILE_InitDebug.argtypes = [C.c_char_p, i32]
    lib.CAJFILE_MiniatureEx2.restype = vp
    lib.CAJFILE_MiniatureEx2.argtypes = [vp, i32, C.c_float]


def load(lib_path=None, res_base=None, base_font=None, debug_log=None,
         debug_level=3, quiet=True):
    """Load the .so, run CAJFILE_Init, return (lib, base_dir).

    `base_font` is the fallback family every unresolved CJK lookup falls back to.
    Pass "" to skip the call entirely (and get the inkless render). The library
    latches the first successful SetBaseFont for the whole process, so set it here
    rather than per-document.
    """
    lib_path, base = find_library(lib_path, res_base)
    lib = C.CDLL(lib_path)
    _bind(lib)
    if debug_log:
        lib.CAJFILE_InitDebug(os.fsencode(debug_log), debug_level)
    # arg1 is the base directory (see module docstring). arg4 must be a valid
    # string: GlobalParams' ctor strcpy's it, so NULL segfaults in __strcpy_evex.
    b = base.encode("utf-8", "surrogateescape")
    rc = lib.CAJFILE_Init(b, b, 0, b)
    if rc != 0:
        raise CAJError("CAJFILE_Init(%r) -> %d" % (base, rc))
    if base_font is None:
        base_font = os.environ.get("CAJ_BASE_FONT", DEFAULT_BASE_FONT)
    if base_font:
        # Void return -- the only way to tell it worked is to render and count ink.
        lib.CAJFILE_SetBaseFont(base_font.encode("utf-8", "surrogateescape"))
    return lib, base


def open_doc(path, lib=None, lib_path=None, res_base=None, base_font=None):
    """Open a .caj file and return a CAJDoc."""
    if lib is None:
        lib, _base = load(lib_path, res_base, base_font=base_font)
    h = lib.CAJFILE_Open(os.fsencode(path))
    if not h:
        raise CAJError("CAJFILE_Open failed for %r" % (path,))
    return CAJDoc(lib, h)


# --------------------------------------------------------------------------
# unit helpers
# --------------------------------------------------------------------------
UNITS_PER_PT = 100.0


def units_to_pt(u):
    return u / UNITS_PER_PT


def units_to_px(u, dpi):
    return u * dpi / 7200.0


__all__ = [
    "CAJDoc", "CAJError", "TextRun", "load", "open_doc", "find_library",
    "units_to_pt", "units_to_px", "UNITS_PER_PT", "LIBNAME",
    "DEFAULT_BASE_FONT", "LOGPAGE_SIZE", "LOGPAGE_FIELDS",
]
