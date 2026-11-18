#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wfpdf_pdfwriter.py -- a minimal but STRICTLY CORRECT PDF 1.7 writer.

Pure standard library (zlib / struct / hashlib / io / math / os).  No pypdf,
no reportlab, no fontTools.  `numpy` is used *only* as an optional accelerator
for the PNG-predictor row filter; the module imports and works without it.

Everything below is hand rolled:

  * object model + byte-accurate serialization (offsets are recorded from
    ``len(buffer)`` at write time, never from a counter),
  * classic cross-reference TABLE (20-byte entries, single subsection),
  * streams with correct /Length (direct *or* indirect) and /FlateDecode
    (selectable level) or no filter at all,
  * Catalog -> Pages -> Page with /MediaBox, /Resources, /Contents,
  * image XObjects: lossless DeviceRGB/DeviceGray + FlateDecode with PNG-style
    predictor scanlines (per-row choice of filter 0/1/2 by min-sum-of-abs), and
    lossy DCTDecode from caller supplied JPEG bytes,
  * Type0 / CIDFontType2 composite font for an embedded TrueType subset, with
    compact /W ranges, FontDescriptor in 1/1000 em, /FontFile2 with /Length1,
  * ToUnicode CMap (bfchar + bfrange, <=100 entries per block, UTF-16BE),
  * text primitives: absolute ``Tm``+``Tj`` and kerned ``TJ`` arrays whose
    numeric adjustments make every glyph land on a requested absolute x,
  * strict escaping: PDF names (#XX for everything outside 0x21..0x7E plus the
    delimiters), literal strings (\\ ( ) and \\nnn octal), hex strings,
    locale independent fixed-precision float formatting.

--------------------------------------------------------------------------
VALUE ENCODING RULES for dictionaries / arrays  (function ``pdf_encode``)
--------------------------------------------------------------------------
    None            -> null
    bool            -> true / false          (checked before int!)
    int             -> 123
    float           -> fixed precision, trailing zeros stripped ("0" for 0.0)
    str             -> a NAME; a leading "/" is stripped, then every byte
                       outside 0x21..0x7E and every delimiter ()<>[]{}/%# is
                       written as #XX  ("/Identity-H", "Identity-H" are equal)
    bytes           -> a LITERAL string, escaped: \\ ( ) and \\nnn octal
    Literal(s|b)    -> a LITERAL string; a str is encoded ASCII when it is
                       pure ASCII, otherwise UTF-16BE with a \\xFE\\xFF BOM
    HexStr(b)       -> a HEX string <....>
    Ref(num, gen)   -> "num gen R"  (also: any object returned by
                       PDFDocument.make_indirect / PDFObject.ref)
    Raw(tok)        -> copied verbatim (already-serialized token(s))
    list / tuple    -> array
    dict            -> dictionary; KEYS are str names without the leading "/"
    PDFStream       -> stream object body (only valid as an object's value)

--------------------------------------------------------------------------
PUBLIC API (exact signatures)
--------------------------------------------------------------------------
Module level helpers
    pdf_name(name) -> bytes                     "/A#20B" style
    pdf_format_number(value, precision=6) -> str
    escape_literal(data: bytes) -> bytes        b"(...)"
    hex_string(data: bytes) -> bytes            b"<...>"
    utf16be_hex(codepoint: int) -> bytes        b"4E2D" (surrogate-safe)
    png_filter_scanlines(raw, width, height, colors=3, filters=(0,1,2))
        -> (filtered_bytes, per_row_filter_tuple)
    png_unfilter_scanlines(filtered, width, height, colors=3) -> bytes
    compact_w_array(widths: dict[int,int], dw: int, max_array=64) -> list[str]
    build_tounicode_cmap(gid_to_codes: dict[int, tuple[int,...]]) -> bytes
    build_text_run(font_key, widths_1000, glyph_ids, xs_pt, size_pt, y_pt,
                   *, x0=None, precision=3, use_tj=True) -> bytes
        whole "BT ... ET" block (no trailing newline handling surprises: it
        ends with b"ET\\n").
    jpeg_sof_info(jpeg_bytes) -> (width, height, ncomponents)
    parse_ttf_metrics(ttf_bytes) -> dict        values in FONT UNITS (upem)
    flate(data, level) / deflated(data, level)

class PDFDocument
    def __init__(self, title=None, author=None, producer=None, creator=None,
                 subject=None, keywords=None, version='1.7',
                 float_precision=6, content_precision=3, compress_level=9,
                 compress_streams=True, creation_date=None, doc_id=None,
                 indirect_lengths=False)
    pages: list[Page]
    def add_page(self, width_pt: float, height_pt: float) -> Page
    def make_indirect(self, value) -> Ref
    def serialize(self) -> bytes
    def save(self, path_or_fileobj, *, compress_level=None) -> int   # bytes written
    # introspection filled in by save()/serialize()
    offsets: dict[int,int]; xref_offset: int; file_size: int

class Page
    mediabox: list[float]      # [0, 0, width_pt, height_pt]
    rotation: int              # emitted as /Rotate only when != 0
    procset: list[str]
    resources: dict            # {'XObject': {...}, 'Font': {...}, ...}
    key: str                   # 'Pg1' ... (unused by the file, handy for logs)
    def add_image(self, data, w=None, h=None, x=0.0, y=None, wpt=None,
                  hpt=None, *, y_top=None, mode='rgb', name=None,
                  flate_data=None, decode_parms=None, dct_data=None,
                  level=None, smask=None, place=True) -> str   # 'Im0'
    def add_image_xobject(...) -> str        # same, place=False by default
    def add_truetype_font(self, name, subset_bytes, code_to_gid, gid_to_adv,
                          upem=2048, metrics=None, *, resource_key=None,
                          base_font=None, subset_tag=None, flags=4,
                          stem_v=None, compress_font=True, level=None,
                          indirect_length=None) -> Font
    def place_xobject(self, key, x, y, wpt, hpt) -> None
    def y_from_top(self, y_top_pt, h_pt=0.0) -> float
    # content stream primitives (all append to the page's content stream)
    def raw(self, data: bytes) -> None
    def begin_text(self) / end_text(self)
    def set_font(self, font_or_key, size_pt) / set_text_matrix(a,b,c,d,e,f)
    def set_text_pos(self, x, y) / set_leading(v) / next_line()
    def set_char_space(v) / set_word_space(v) / set_horiz_scale(v)
    def show_literal(self, text) / show_hex(self, gid_bytes_or_ids, font=None)
    def show_tj(self, parts)
    def show_text_at(self, font, glyph_ids, x_pt, y_pt, size_pt, *,
                     hex_string=True, use_tj=False)   -> bytes  (BT..ET)
    def show_run(self, font, glyph_ids, xs_pt, size_pt, y_pt, *, x0=None,
                 use_tj=True, precision=None) -> bytes  (BT..ET, kerned TJ)
    def set_fill_rgb(r,g,b) / set_fill_gray(g) / set_stroke_rgb(r,g,b)
    def rect(x,y,w,h) / fill() / stroke() / fill_stroke()
    def content_bytes(self) -> bytes        # what will go into /Contents

class Font
    key: str                  # resource key, e.g. 'F1'
    base_font: str            # e.g. 'ABCDEF+WQYMicroHe'
    upem: int
    code_to_gid: dict[int,int]
    gid_to_adv: dict[int,int]      # FONT UNITS
    widths: dict[int,int]          # 1/1000 em, == round(adv*1000/upem)
    dw: int                        # /DW, most common width (1/1000 em)
    ref / cidfont_ref / descriptor_ref / fontfile_ref / tounicode_ref: Ref
    ttf_bytes: bytes
    tounicode_bytes: bytes
    missing: set[int]         # codepoints asked for that had no gid
    def gid(self, codepoint) -> int
    def gids(self, text) -> list[int]
    def width_1000(self, gid) -> int
    def advance_pt(self, gid, size_pt) -> float
    def ascender_pt(self, size_pt) -> float      # baseline offset helper

COORDINATES
    PDF user space, origin BOTTOM-LEFT, units = PostScript points.  The CAJ
    source data is top-left origin in 1/100 pt units; convert with
        x_pt   = x_units / 100.0
        y_pt   = page_height_pt - (y_top_units / 100.0) - h_pt
    Page.y_from_top() and Font.ascender_pt() are provided as conveniences.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import struct
import zlib

try:                                   # OPTIONAL accelerator, never required
    import numpy as _np
except Exception:                      # pragma: no cover
    _np = None

__all__ = [
    'PDFDocument', 'Page', 'Font', 'PDFStream', 'Ref', 'Raw', 'Literal',
    'HexStr', 'pdf_name', 'pdf_encode', 'pdf_format_number', 'escape_literal',
    'hex_string', 'utf16be_hex', 'png_filter_scanlines',
    'png_unfilter_scanlines', 'compact_w_array', 'build_tounicode_cmap',
    'build_text_run', 'jpeg_sof_info', 'parse_ttf_metrics', 'deflated',
]

PDF_VERSION_DEFAULT = '1.7'
_NAME_DELIMS = frozenset(b'()<>[]{}/%#')


# --------------------------------------------------------------------------
# low level token helpers
# --------------------------------------------------------------------------
def pdf_name(name) -> bytes:
    """Serialize a PDF *name*.  Accepts str or bytes, with or without '/'."""
    if isinstance(name, str):
        raw = name.encode('utf-8')
    elif isinstance(name, (bytes, bytearray)):
        raw = bytes(name)
    else:
        raise TypeError('pdf_name needs str/bytes, got %r' % (type(name),))
    if raw[:1] == b'/':
        raw = raw[1:]
    out = bytearray(b'/')
    for b in raw:
        if 0x21 <= b <= 0x7E and b not in _NAME_DELIMS:
            out.append(b)
        else:
            out += b'#%02X' % b
    return bytes(out)


def pdf_format_number(value, precision: int = 6) -> str:
    """Locale independent fixed-precision number -> str, no trailing garbage."""
    if isinstance(value, bool):
        raise TypeError('bool is not a number here')
    if isinstance(value, int):
        return str(value)
    f = float(value)
    if math.isnan(f) or math.isinf(f):
        raise ValueError('cannot serialize %r as a PDF number' % (f,))
    if f == 0.0:
        return '0'
    if precision < 0:
        raise ValueError('precision must be >= 0')
    s = '%.*f' % (precision, f)          # '%.3f' is locale independent
    if '.' in s:
        s = s.rstrip('0').rstrip('.')
    if s == '' or s == '-0' or s == '-':
        s = '0'
    return s


def escape_literal(data) -> bytes:
    """Serialize a PDF literal string b'(...)' with correct escaping."""
    if isinstance(data, str):
        data = Literal.encode_text(data)
    data = bytes(data)
    out = bytearray(b'(')
    for b in data:
        if b == 0x28 or b == 0x29 or b == 0x5C:      # ( ) backslash
            out.append(0x5C)
            out.append(b)
        elif 0x20 <= b <= 0x7E:
            out.append(b)
        else:
            out += b'\\%03o' % b                     # always 3 octal digits
    out.append(0x29)
    return bytes(out)


def hex_string(data) -> bytes:
    """Serialize a PDF hexadecimal string b'<...>' (upper case, even length)."""
    return b'<' + bytes(data).hex().upper().encode('ascii') + b'>'


def utf16be_hex(codepoint: int) -> str:
    """UTF-16BE hex (no angle brackets) of one codepoint, surrogate aware."""
    if not 0 <= codepoint <= 0x10FFFF:
        raise ValueError('bad codepoint %r' % (codepoint,))
    return chr(codepoint).encode('utf-16-be').hex().upper()


class Ref:
    """An indirect reference: ``num gen R``."""
    __slots__ = ('num', 'gen')

    def __init__(self, num: int, gen: int = 0):
        self.num = int(num)
        self.gen = int(gen)

    def __repr__(self):
        return 'Ref(%d,%d)' % (self.num, self.gen)

    def __eq__(self, other):
        return (isinstance(other, Ref) and other.num == self.num
                and other.gen == self.gen)

    def __hash__(self):
        return hash((self.num, self.gen))


class Raw:
    """Verbatim already-serialized token(s)."""
    __slots__ = ('tok',)

    def __init__(self, tok):
        self.tok = tok if isinstance(tok, bytes) else str(tok).encode('ascii')


class Literal:
    """A PDF literal string built from str (ASCII / UTF-16BE+BOM) or bytes."""
    __slots__ = ('data',)

    def __init__(self, data):
        self.data = data

    @staticmethod
    def encode_text(s: str) -> bytes:
        if all(ord(c) < 128 for c in s):
            return s.encode('ascii')
        return b'\xfe\xff' + s.encode('utf-16-be')

    def encoded(self) -> bytes:
        if isinstance(self.data, str):
            return self.encode_text(self.data)
        return bytes(self.data)


class HexStr:
    """A PDF hexadecimal string."""
    __slots__ = ('data',)

    def __init__(self, data):
        self.data = bytes(data)


def deflated(data, level: int = 9) -> bytes:
    """zlib/flate compress (raw zlib stream, as PDF's FlateDecode wants)."""
    if level is None:
        level = 9
    return zlib.compress(bytes(data), level)


def pdf_encode(value, precision: int = 6) -> bytes:
    """Serialize any value per the encoding rules in the module docstring."""
    out = bytearray()
    _encode_into(value, out, precision)
    return bytes(out)


def _encode_into(v, out: bytearray, prec: int) -> None:
    t = type(v)
    if v is None:
        out += b'null'
    elif t is bool:
        out += b'true' if v else b'false'
    elif t is int:
        out += str(v).encode('ascii')
    elif t is float:
        out += pdf_format_number(v, prec).encode('ascii')
    elif t is str:
        out += pdf_name(v)
    elif t is bytes or t is bytearray:
        out += escape_literal(bytes(v))
    elif t is Ref:
        out += ('%d %d R' % (v.num, v.gen)).encode('ascii')
    elif t is Raw:
        out += v.tok
    elif t is Literal:
        out += escape_literal(v.encoded())
    elif t is HexStr:
        out += hex_string(v.data)
    elif t is list or t is tuple:
        out += b'['
        first = True
        for item in v:
            if not first:
                out += b' '
            first = False
            _encode_into(item, out, prec)
        out += b']'
    elif t is dict:
        out += b'<<'
        for key, val in v.items():
            if not isinstance(key, str):
                raise TypeError('dictionary keys must be str names, got %r'
                                % (key,))
            out += b' '
            out += pdf_name(key)
            out += b' '
            _encode_into(val, out, prec)
        out += b' >>'
    else:
        raise TypeError('cannot serialize %r' % (t,))


# --------------------------------------------------------------------------
# streams
# --------------------------------------------------------------------------
class PDFStream:
    """A stream object body: a dictionary plus bytes.

    ``entries``   dict of dictionary entries WITHOUT /Length and /Filter
                  (those are derived from the parameters below; you may still
                  put /Filter in yourself by passing ``filter_name=None``).
    ``data``      uncompressed bytes (ignored when ``flate_data`` is given).
    ``compress``  True -> /FlateDecode over ``data``; False -> raw stream.
    ``pre_compressed``  when True, ``data`` is already the filtered stream
                  bytes and is written verbatim (``filter_name`` still emitted).
    ``indirect_length``  emit /Length as an indirect object reference.
    """

    def __init__(self, data=b'', entries=None, *, compress=True, level=9,
                 pre_compressed=False, indirect_length=False, filter_name=None,
                 decode_parms=None):
        self.entries = dict(entries or {})
        self.data = bytes(data)
        self.compress = bool(compress)
        self.level = level
        self.pre_compressed = bool(pre_compressed)
        self.indirect_length = bool(indirect_length)
        self.filter_name = filter_name
        self.decode_parms = decode_parms
        self._length_ref = None
        self._encoded = None

    def encoded_bytes(self):
        """The bytes that go between 'stream' and 'endstream'."""
        if self._encoded is None:
            if self.pre_compressed:
                self._encoded = self.data
            elif self.compress:
                self._encoded = deflated(self.data, self.level)
            else:
                self._encoded = self.data
        return self._encoded

    def full_dict(self, precision: int = 6) -> dict:
        d = dict(self.entries)
        payload = self.encoded_bytes()
        d['Length'] = (Ref(*self._length_ref) if self._length_ref
                       else len(payload))
        tok = self._filter_tokens()
        if tok is not None:
            d['Filter'] = tok
        if self.decode_parms is not None:
            d['DecodeParms'] = self.decode_parms
        return d

    def _filter_tokens(self):
        if self.filter_name is not None:
            return self.filter_name
        if self.pre_compressed:
            return None
        return '/FlateDecode' if self.compress else None


# --------------------------------------------------------------------------
# PNG-style predictor filtering (lossless image XObjects)
# --------------------------------------------------------------------------
def _row_costs_and_candidates(row: bytes, prev: bytes, bpp: int,
                              filters=(0, 1, 2)):
    """Return list of (filter_type, filtered_bytes, cost) for one scanline."""
    n = len(row)
    out = []
    for ft in filters:
        if ft == 0:
            cand = row
        elif ft == 1:                                   # Sub
            buf = bytearray(n)
            for i in range(n):
                a = row[i - bpp] if i >= bpp else 0
                buf[i] = (row[i] - a) & 0xFF
            cand = bytes(buf)
        elif ft == 2:                                   # Up
            cand = bytes((a - b) & 0xFF for a, b in zip(row, prev))
        elif ft == 3:                                   # Average
            buf = bytearray(n)
            for i in range(n):
                a = row[i - bpp] if i >= bpp else 0
                buf[i] = (row[i] - ((a + prev[i]) >> 1)) & 0xFF
            cand = bytes(buf)
        elif ft == 4:                                   # Paeth
            buf = bytearray(n)
            for i in range(n):
                a = row[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                b = prev[i]
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                buf[i] = (row[i] - pr) & 0xFF
            cand = bytes(buf)
        else:
            raise ValueError('unknown PNG filter type %r' % (ft,))
        # PNG heuristic: treat each filtered byte as signed, sum |value|
        cost = sum(b if b < 128 else 256 - b for b in cand)
        out.append((ft, cand, cost))
    return out


def png_filter_scanlines(raw, width: int, height: int, colors: int = 3,
                         filters=(0, 1, 2), level: int = None):
    """Convert raw interleaved samples into PNG-style filtered scanlines.

    Each output row is prefixed by a single filter-type byte.  The filter is
    chosen per row by the minimum sum-of-absolute-differences heuristic over
    ``filters`` (default 0=None, 1=Sub, 2=Up), which is what makes text pages
    compress well.  Row 0 breaks ties in favour of the smallest filter type.

    Returns ``(filtered_bytes, tuple_of_chosen_filter_types)``.
    """
    raw = bytes(raw)
    stride = width * colors
    if len(raw) != stride * height:
        raise ValueError('raw image is %d bytes, expected %d*%d*%d = %d'
                         % (len(raw), height, width, colors, stride * height))
    if stride == 0:
        raise ValueError('zero stride')
    filters = tuple(filters)
    out = bytearray()
    chosen = []

    if _np is not None and stride >= 8:
        arr = _np.frombuffer(raw, dtype=_np.uint8).reshape(height, stride)
        prev16 = _np.zeros(stride, dtype=_np.int16)
        for y in range(height):
            cur = arr[y]
            cur16 = cur.astype(_np.int16)
            best = None
            for ft in filters:
                if ft == 0:
                    diff = cur16
                elif ft == 1:
                    bpp = colors
                    left = _np.empty(stride, dtype=_np.int16)
                    left[:bpp] = 0
                    left[bpp:] = cur16[:-bpp]
                    diff = cur16 - left
                elif ft == 2:
                    diff = cur16 - prev16
                else:                                  # fall back: pure python
                    diff = None
                if diff is None:
                    cand_list = _row_costs_and_candidates(
                        cur.tobytes(), prev16.astype(_np.uint8).tobytes(),
                        colors, (ft,))
                    ft_, cand, cost = cand_list[0]
                else:
                    cand = (diff & 0xFF).astype(_np.uint8).tobytes()
                    cost = int(_np.abs(diff).sum())
                if best is None or cost < best[2]:
                    best = (ft, cand, cost)
            ft, cand, _ = best
            out.append(ft)
            out += cand
            chosen.append(ft)
            prev16 = cur16
        return bytes(out), tuple(chosen)

    prev = bytes(stride)
    for y in range(height):
        row = raw[y * stride:(y + 1) * stride]
        cands = _row_costs_and_candidates(row, prev, colors, filters)
        ft, cand, _cost = min(cands, key=lambda t: (t[2], t[0]))
        out.append(ft)
        out += cand
        chosen.append(ft)
        prev = row
    return bytes(out), tuple(chosen)


def png_unfilter_scanlines(filtered, width: int, height: int,
                           colors: int = 3) -> bytes:
    """Inverse of png_filter_scanlines.  Handles filter types 0..4."""
    filtered = bytes(filtered)
    stride = width * colors
    bpp = colors
    need = height * (stride + 1)
    if len(filtered) != need:
        raise ValueError('filtered data is %d bytes, expected %d'
                         % (len(filtered), need))
    out = bytearray()
    prev = bytes(stride)
    pos = 0
    for _y in range(height):
        ft = filtered[pos]
        pos += 1
        cur = filtered[pos:pos + stride]
        pos += stride
        recon = bytearray(stride)
        if ft == 0:
            recon[:] = cur
        elif ft == 1:
            for i in range(stride):
                a = recon[i - bpp] if i >= bpp else 0
                recon[i] = (cur[i] + a) & 0xFF
        elif ft == 2:
            for i in range(stride):
                recon[i] = (cur[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = recon[i - bpp] if i >= bpp else 0
                recon[i] = (cur[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = recon[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                recon[i] = (cur[i] + pr) & 0xFF
        else:
            raise ValueError('unknown filter type %d' % ft)
        out += recon
        prev = bytes(recon)
    return bytes(out)


# --------------------------------------------------------------------------
# /W compaction, ToUnicode CMap, text runs
# --------------------------------------------------------------------------
def compact_w_array(widths, dw: int, max_array: int = 64) -> list:
    """Build the tokens of a CIDFont /W array, excluding entries equal to /DW.

    ``widths``: {gid: width in 1/1000 em}.  Returns a list of str tokens:
    either ``"first last w"`` (equal-width run of >= 2 consecutive gids) or
    ``"first [w1 w2 ...]"`` (mixed widths, at most ``max_array`` per array).
    """
    items = [(g, w) for g, w in sorted(widths.items()) if w != dw]
    tokens = []
    i = 0
    n = len(items)
    while i < n:
        g0, w0 = items[i]
        j = i + 1
        while j < n and items[j][0] == items[j - 1][0] + 1 and items[j][1] == w0:
            j += 1
        if j - i >= 2:                                  # range form
            tokens.append('%d %d %d' % (g0, items[j - 1][0], w0))
            i = j
            continue
        arr = [w0]                                      # array form
        k = i + 1
        while k < n and len(arr) < max_array and items[k][0] == items[k - 1][0] + 1:
            peek = k
            while (peek < n and items[peek][0] == items[peek - 1][0] + 1
                   and items[peek][1] == items[k][1]):
                peek += 1
            if peek - k >= 2:
                break                                   # let range form take it
            arr.append(items[k][1])
            k += 1
        tokens.append('%d [%s]' % (g0, ' '.join(str(x) for x in arr)))
        i = k
    return tokens


def build_tounicode_cmap(gid_to_codes) -> bytes:
    """Build a ToUnicode CMap mapping 2-byte CIDs (== our GIDs) to UTF-16BE.

    ``gid_to_codes``: {gid: (codepoint, ...)}; a multi-codepoint tuple becomes
    one bfchar entry whose value is the concatenated UTF-16BE hex.  Consecutive
    gids mapping to consecutive codepoints are folded into bfrange entries.
    Both block types hold at most 100 entries (the spec's limit).
    """
    items = sorted((int(g), tuple(codes)) for g, codes in gid_to_codes.items())
    # ---- bfrange candidates: gid runs with codepoint runs of length 1 values
    ranges = []          # (gfirst, glast, code_first)
    chars = []           # (gid, (codes...))
    i = 0
    n = len(items)
    while i < n:
        g, codes = items[i]
        if len(codes) == 1:
            j = i + 1
            while (j < n and items[j][0] == items[j - 1][0] + 1
                   and len(items[j][1]) == 1
                   and items[j][1][0] == items[j - 1][1][0] + 1
                   and items[j][1][0] <= 0xFFFF):
                j += 1
            if j - i >= 2 and codes[0] <= 0xFFFF:
                ranges.append((g, items[j - 1][0], codes[0]))
                i = j
                continue
        chars.append((g, codes))
        i += 1

    lines = []
    a = lines.append
    a(b'/CIDInit /ProcSet findresource begin')
    a(b'12 dict begin')
    a(b'begincmap')
    a(b'/CIDSystemInfo')
    a(b'<< /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def')
    a(b'/CMapName /Adobe-Identity-UCS def')
    a(b'/CMapType 2 def')
    a(b'1 begincodespacerange')
    a(b'<0000> <FFFF>')
    a(b'endcodespacerange')

    for start in range(0, len(ranges), 100):
        chunk = ranges[start:start + 100]
        a(b'%d beginbfrange' % len(chunk))
        for gfirst, glast, cfirst in chunk:
            a(b'<%04X> <%04X> <%04X>' % (gfirst, glast, cfirst))
        a(b'endbfrange')

    for start in range(0, len(chars), 100):
        chunk = chars[start:start + 100]
        a(b'%d beginbfchar' % len(chunk))
        for g, codes in chunk:
            val = ''.join(utf16be_hex(c) for c in codes)
            a(b'<%04X> <%s>' % (g, val.encode('ascii')))
        a(b'endbfchar')

    a(b'endcmap')
    a(b'CMapName currentdict /CMap defineresource pop')
    a(b'end')
    a(b'end')
    return b'\n'.join(lines) + b'\n'


def build_text_run(font_key: str, widths_1000, glyph_ids, xs_pt, size_pt,
                   y_pt, *, x0=None, precision: int = 3, use_tj: bool = True,
                   per_glyph: bool = False, matrix=None) -> bytes:
    """Build a complete ``BT ... ET`` block that places every glyph exactly.

    ``widths_1000``  {gid: width in 1/1000 em} -- MUST be the same integers
                   that are written into the CIDFont's /W (and /DW), otherwise
                   a reader reconstructing positions will drift.
    ``xs_pt``      absolute x of each glyph's pen position, in points.
    ``size_pt``    the Tf size.
    ``y_pt``       baseline y in PDF user space (bottom-left origin).
    ``x0``         explicit Tm x origin.  Defaults to ``xs_pt[0]``; when it
                   differs, a leading TJ adjustment moves the first glyph onto
                   ``xs_pt[0]``.
    ``use_tj``     True (default) -> one BT..ET with a kerned TJ array.
                   False -> one BT..ET with a single ``Tm`` and ONE hex string
                   holding all glyphs (natural advances; the plain
                   "BT /F1 s Tf 1 0 0 1 x y Tm <string> Tj ET" form).
    ``per_glyph``  only meaningful with ``use_tj=False``: emit one BT..ET per
                   glyph, each with its own absolute ``Tm`` at ``xs_pt[i]``.

    Kerning math (per the CAJ spec we were given):
        advance_user = W_i/1000 * Tf
        d            = x_{i+1} - (x_i + advance_user)
        t            = -1000 * d / Tf
    Assumes Tz=100, Tc=Tw=0 and an unrotated text matrix.
    """
    gids = list(glyph_ids)
    xs = [float(v) for v in xs_pt]
    if len(gids) != len(xs):
        raise ValueError('glyph/x count mismatch: %d vs %d'
                         % (len(gids), len(xs)))
    if not gids:
        return b''
    size = float(size_pt)
    if size <= 0:
        raise ValueError('font size must be > 0')
    key = font_key if isinstance(font_key, bytes) else pdf_name(font_key)
    prec = int(precision)

    def n(v):
        return pdf_format_number(v, prec).encode('ascii')

    if not use_tj and per_glyph:
        out = bytearray()
        for g, x in zip(gids, xs):
            out += (b'BT ' + key + b' ' + n(size) + b' Tf 1 0 0 1 ' + n(x)
                    + b' ' + n(float(y_pt)) + b' Tm '
                    + hex_string(struct.pack('>H', g)) + b' Tj ET\n')
        return bytes(out)

    if not use_tj:
        e = xs[0] if x0 is None else float(x0)
        return (b'BT ' + key + b' ' + n(size) + b' Tf 1 0 0 1 ' + n(e) + b' '
                + n(float(y_pt)) + b' Tm '
                + hex_string(struct.pack('>%dH' % len(gids), *gids))
                + b' Tj ET\n')

    if matrix is None:
        e = xs[0] if x0 is None else float(x0)
        f = float(y_pt)
    else:
        e, f = float(matrix[4]), float(matrix[5])
    out = bytearray()
    out += b'BT ' + key + b' ' + n(size) + b' Tf 1 0 0 1 ' + n(e) + b' '
    out += n(f) + b' Tm '

    parts = []
    if x0 is not None and float(x0) != xs[0]:
        t_lead = -1000.0 * (xs[0] - float(x0)) / size
        s = pdf_format_number(t_lead, prec)
        if s != '0':
            parts.append(s.encode('ascii'))

    cur = [gids[0]]
    scale = 1000.0 / size
    for i in range(1, len(gids)):
        w = widths_1000.get(gids[i - 1])
        if w is None:
            raise KeyError('no width for gid %d' % (gids[i - 1],))
        advance_user = (w / 1000.0) * size
        d = xs[i] - (xs[i - 1] + advance_user)
        t = -d * scale
        s = pdf_format_number(t, prec)
        if s == '0':
            cur.append(gids[i])
        else:
            parts.append(hex_string(struct.pack('>%dH' % len(cur), *cur)))
            parts.append(s.encode('ascii'))
            cur = [gids[i]]
    parts.append(hex_string(struct.pack('>%dH' % len(cur), *cur)))
    out += b'[' + b' '.join(parts) + b'] TJ ET\n'
    return bytes(out)


# --------------------------------------------------------------------------
# JPEG / TrueType introspection (pure struct)
# --------------------------------------------------------------------------
def jpeg_sof_info(data) -> tuple:
    """Parse a JPEG's SOF marker -> (width, height, ncomponents)."""
    d = bytes(data)
    if d[:2] != b'\xff\xd8':
        raise ValueError('not a JPEG (no SOI)')
    i = 2
    n = len(d)
    while i + 9 <= n:
        if d[i] != 0xFF:
            i += 1
            continue
        while i < n and d[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = d[i]
        i += 1
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9 or marker == 0xDA:      # EOI / SOS: stop scanning
            break
        seglen = struct.unpack('>H', d[i:i + 2])[0]
        body = d[i + 2:i + seglen]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if len(body) < 6:
                raise ValueError('truncated SOF%d' % (marker - 0xC0))
            prec = body[0]
            h, w = struct.unpack('>HH', body[1:5])
            ncomp = body[5]
            if prec != 8:
                raise ValueError('unsupported JPEG precision %d' % prec)
            return (w, h, ncomp)
        i += seglen
    raise ValueError('no SOF marker found in JPEG')


def jpeg_is_adobe(data) -> bool:
    d = bytes(data)
    i = 2
    n = len(d)
    while i + 4 <= n:
        if d[i] != 0xFF:
            break
        marker = d[i + 1]
        if marker == 0xDA or marker == 0xD9:
            break
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = struct.unpack('>H', d[i + 2:i + 4])[0]
        if marker == 0xEE:
            body = d[i + 4:i + 2 + seglen]
            if body[:5] == b'Adobe' and len(body) >= 12:
                return True
        i += 2 + seglen
    return False


def _ttf_dir(d: bytes) -> dict:
    num = struct.unpack('>H', d[4:6])[0]
    recs = {}
    for i in range(num):
        o = 12 + 16 * i
        tag = d[o:o + 4].decode('latin-1')
        _cs, off, length = struct.unpack('>3I', d[o + 4:o + 16])
        recs[tag] = (off, length)
    return recs


def parse_ttf_metrics(ttf: bytes) -> dict:
    """Read the metrics a PDF FontDescriptor needs, in FONT UNITS (upem).

    Returns a dict with keys: upem, numGlyphs, bbox (4 ints), ascent, descent,
    lineGap, capHeight, xHeight, italicAngle, avgWidth, maxWidth, weightClass,
    stemV (heuristic), flags (heuristic), underlinePosition,
    underlineThickness.  Missing tables degrade to sane defaults rather than
    raising, except head/hhea/maxp which are mandatory.
    """
    d = bytes(ttf)
    if len(d) < 12:
        raise ValueError('not a TrueType font (too short)')
    sfnt = struct.unpack('>I', d[0:4])[0]
    if sfnt not in (0x00010000, 0x74727565):
        raise ValueError('not a TrueType sfnt (tag 0x%08X); CFF/OpenType is not '
                         'supported by /FontFile2' % sfnt)
    recs = _ttf_dir(d)

    def tbl(tag):
        off, ln = recs[tag]
        return d[off:off + ln]

    head = tbl('head')
    hhea = tbl('hhea')
    maxp = tbl('maxp')
    upem = struct.unpack('>H', head[18:20])[0]
    bbox = list(struct.unpack('>4h', head[36:44]))
    num_glyphs = struct.unpack('>H', maxp[4:6])[0]
    ascent, descent, line_gap = struct.unpack('>3h', hhea[4:10])
    adv_max = struct.unpack('>H', hhea[10:12])[0]

    m = {
        'upem': upem,
        'numGlyphs': num_glyphs,
        'bbox': bbox,
        'ascent': ascent,
        'descent': descent,
        'lineGap': line_gap,
        'capHeight': ascent,
        'xHeight': 0,
        'italicAngle': 0.0,
        'avgWidth': adv_max,
        'maxWidth': adv_max,
        'weightClass': 400,
        'underlinePosition': -upem // 8,
        'underlineThickness': upem // 20,
    }
    if 'OS/2' in recs:
        os2 = tbl('OS/2')
        ver = struct.unpack('>H', os2[0:2])[0]
        m['avgWidth'] = struct.unpack('>h', os2[2:4])[0]
        m['weightClass'] = struct.unpack('>H', os2[4:6])[0]
        if ver >= 2 and len(os2) >= 90:
            m['xHeight'] = struct.unpack('>h', os2[86:88])[0]
            m['capHeight'] = struct.unpack('>h', os2[88:90])[0] or ascent
        else:
            m['capHeight'] = ascent
        typo_asc, typo_desc = struct.unpack('>2h', os2[68:72])
        m['typoAscent'] = typo_asc
        m['typoDescent'] = typo_desc
        m['winAscent'], m['winDescent'] = struct.unpack('>2H', os2[74:78])
    else:
        m['capHeight'] = ascent
    if 'post' in recs:
        post = tbl('post')
        m['italicAngle'] = struct.unpack('>i', post[4:8])[0] / 65536.0
        if len(post) >= 12:
            m['underlinePosition'] = struct.unpack('>h', post[8:10])[0]
            m['underlineThickness'] = struct.unpack('>h', post[10:12])[0]
    # StemV heuristic (font units): ~6% of the em, thicker for bold weights
    wc = m['weightClass']
    m['stemV'] = max(20, int(round(upem * (0.05 + 0.00005 * max(0, wc - 400)))))
    m['stemH'] = max(10, int(round(m['stemV'] * 0.6)))
    m['flags'] = 4                       # symbolic: a non-Latin CID-keyed font
    return m


def ttf_advance_table(ttf: bytes):
    """Return (advances_list, numGlyphs) honouring numberOfHMetrics < numGlyphs."""
    d = bytes(ttf)
    recs = _ttf_dir(d)
    num_glyphs = struct.unpack('>H', d[recs['maxp'][0] + 4:recs['maxp'][0] + 6])[0]
    hhea = d[recs['hhea'][0]:recs['hhea'][0] + recs['hhea'][1]]
    nhm = struct.unpack('>H', hhea[34:36])[0]
    hmtx = d[recs['hmtx'][0]:recs['hmtx'][0] + recs['hmtx'][1]]
    advs = []
    last = 0
    for g in range(num_glyphs):
        if g < nhm and 4 * g + 2 <= len(hmtx):
            last = struct.unpack('>H', hmtx[4 * g:4 * g + 2])[0]
        advs.append(last)
    return advs, num_glyphs


# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------
class Font:
    """A Type0 / CIDFontType2 composite font backed by an embedded TTF subset.

    Identity-H encoding, /CIDToGIDMap /Identity: a 2-byte code in a content
    stream IS the TrueType glyph id, so ``code_to_gid`` maps a Unicode
    codepoint to the gid that must appear in the hex string.
    """

    def __init__(self, doc, key, base_font, ttf_bytes, code_to_gid, gid_to_adv,
                 upem, metrics, refs, tounicode_bytes, widths, dw, ttf_length):
        self.doc = doc
        self.key = key
        self.base_font = base_font
        self.ttf_bytes = ttf_bytes
        self.code_to_gid = dict(code_to_gid)
        self.gid_to_adv = dict(gid_to_adv)
        self.upem = int(upem)
        self.metrics = dict(metrics or {})
        self.widths = dict(widths)          # gid -> 1/1000 em
        self.dw = int(dw)
        self.tounicode_bytes = tounicode_bytes
        self.ttf_length = int(ttf_length)
        self.missing = set()
        (self.ref, self.cidfont_ref, self.descriptor_ref,
         self.fontfile_ref, self.tounicode_ref) = refs
        self._gid_to_codes = None

    # -- character level helpers ------------------------------------------
    def gid(self, codepoint) -> int:
        if isinstance(codepoint, str):
            codepoint = ord(codepoint)
        g = self.code_to_gid.get(codepoint)
        if g is None:
            self.missing.add(codepoint)
            return 0
        return g

    def gids(self, text) -> list:
        out = []
        for ch in text:
            out.append(self.gid(ord(ch)))
        return out

    def width_1000(self, gid) -> int:
        w = self.widths.get(gid)
        if w is None:
            w = self.gid_to_adv.get(gid)
            w = int(round(w * 1000.0 / self.upem)) if w is not None else self.dw
            self.widths[gid] = w
        return w

    def advance_pt(self, gid, size_pt) -> float:
        return self.width_1000(gid) / 1000.0 * float(size_pt)

    def ascender_pt(self, size_pt) -> float:
        asc = self.metrics.get('ascent', self.upem)
        return asc / float(self.upem) * float(size_pt)

    def descender_pt(self, size_pt) -> float:
        desc = self.metrics.get('descent', 0)
        return desc / float(self.upem) * float(size_pt)

    @property
    def gid_to_codes(self) -> dict:
        if self._gid_to_codes is None:
            inv = {}
            for code, gid in sorted(self.code_to_gid.items()):
                inv.setdefault(gid, []).append(code)
            self._gid_to_codes = {g: tuple(v) for g, v in inv.items()}
        return self._gid_to_codes

    def w_tokens(self, max_array: int = 64) -> list:
        return compact_w_array(self.widths, self.dw, max_array)


_DEFAULT_SUBSET_TAGS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def _subset_tag(doc, base: str) -> str:
    """A deterministic 6-letter ABCDEF+ subset tag."""
    h = hashlib.md5(('%s|%d' % (base, doc._font_serial)).encode('utf-8'))
    v = int(h.hexdigest()[:8], 16)
    return ''.join(_DEFAULT_SUBSET_TAGS[(v >> (5 * i)) % 26] for i in range(6))


def _sanitize_ps_name(name: str, keep_plus: bool = False) -> str:
    """Reduce a family/file name to bytes safe for a PDF name token.

    Keeps ASCII letters, digits, '-' and '_'; with ``keep_plus`` also '+' (so a
    subset tag like ``ABCDEF+Family`` survives).  Everything else -> '-'.
    """
    allowed = set('-_')
    if keep_plus:
        allowed.add('+')
    out = []
    for ch in str(name):
        if ch.isascii() and (ch.isalnum() or ch in allowed):
            out.append(ch)
        else:
            out.append('-')
    return (''.join(out).strip('-') or 'Font')[:90]


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
class Page:
    """One page: MediaBox, Resources, and a content stream you append to."""

    def __init__(self, doc: 'PDFDocument', width_pt: float, height_pt: float):
        self.doc = doc
        self.width_pt = float(width_pt)
        self.height_pt = float(height_pt)
        self.mediabox = [0.0, 0.0, float(width_pt), float(height_pt)]
        self.rotation = 0
        self.procset = ['/PDF', '/Text', '/ImageC']
        self.resources = {}              # kind -> {key -> Ref}
        self._content = bytearray()
        self._in_text = False
        self.ref = None                  # set by the document
        self.key = None
        self._content_ref = None

    # -- geometry ---------------------------------------------------------
    @property
    def width(self):
        return self.mediabox[2] - self.mediabox[0]

    @property
    def height(self):
        return self.mediabox[3] - self.mediabox[1]

    def y_from_top(self, y_top_pt, h_pt: float = 0.0) -> float:
        """Convert a TOP-left-origin y (as CAJ gives) to PDF user space y."""
        return self.mediabox[3] - float(y_top_pt) - float(h_pt)

    # -- content stream ---------------------------------------------------
    def raw(self, data) -> None:
        if isinstance(data, str):
            data = data.encode('latin-1')
        self._content += data

    def content_bytes(self) -> bytes:
        return bytes(self._content)

    def _num(self, v, prec=None) -> bytes:
        p = self.doc.content_precision if prec is None else prec
        return pdf_format_number(v, p).encode('ascii')

    def begin_text(self) -> None:
        if self._in_text:
            raise ValueError('already inside a BT/ET text object')
        self._in_text = True
        self._content += b'BT\n'

    def end_text(self) -> None:
        if not self._in_text:
            raise ValueError('end_text() without begin_text()')
        self._in_text = False
        self._content += b'ET\n'

    def set_font(self, font_or_key, size_pt) -> None:
        key = font_or_key.key if isinstance(font_or_key, Font) else font_or_key
        self._content += pdf_name(key) + b' ' + self._num(size_pt) + b' Tf\n'

    def set_text_matrix(self, a, b, c, d, e, f) -> None:
        self._content += (b' '.join([self._num(v) for v in (a, b, c, d, e, f)])
                          + b' Tm\n')

    def set_text_pos(self, x, y) -> None:
        self._content += self._num(x) + b' ' + self._num(y) + b' Td\n'

    def set_leading(self, v) -> None:
        self._content += self._num(v) + b' TL\n'

    def next_line(self) -> None:
        self._content += b"T*\n"

    def set_char_space(self, v) -> None:
        self._content += self._num(v) + b' Tc\n'

    def set_word_space(self, v) -> None:
        self._content += self._num(v) + b' Tw\n'

    def set_horiz_scale(self, v) -> None:
        self._content += self._num(v) + b' Tz\n'

    def show_literal(self, text) -> None:
        """``Tj`` with a literal string (for simple/latin fonts or debugging)."""
        data = (Literal.encode_text(text) if isinstance(text, str)
                else bytes(text))
        self._content += escape_literal(data) + b' Tj\n'

    def show_hex(self, gids_or_bytes) -> None:
        """``Tj`` with a hex string of 2-byte big-endian gids (or raw bytes)."""
        if isinstance(gids_or_bytes, (bytes, bytearray)):
            data = bytes(gids_or_bytes)
        else:
            data = struct.pack('>%dH' % len(list(gids_or_bytes)),
                               *list(gids_or_bytes))
        self._content += hex_string(data) + b' Tj\n'

    def show_tj(self, parts) -> None:
        """``TJ`` from a list of hex-string bytes / gid lists / number strs."""
        toks = []
        for p in parts:
            if isinstance(p, str):
                toks.append(p.encode('ascii'))
            elif isinstance(p, float):
                toks.append(self._num(p))
            elif isinstance(p, int):
                toks.append(str(p).encode('ascii'))
            elif isinstance(p, (bytes, bytearray)):
                toks.append(bytes(p))
            else:                                   # iterable of gids
                toks.append(hex_string(struct.pack('>%dH' % len(list(p)),
                                                   *list(p))))
        self._content += b'[' + b' '.join(toks) + b'] TJ\n'

    def show_text_at(self, font, glyph_ids, x_pt, y_pt, size_pt, *,
                     use_tj: bool = False, per_glyph: bool = False,
                     precision: int = None) -> bytes:
        """Absolute placement: ``BT /F1 s Tf 1 0 0 1 x y Tm <hex> Tj ET``.

        All glyphs share the single ``Tm`` at (``x_pt``, ``y_pt``) unless
        ``per_glyph=True``, which emits one block per glyph (then ``x_pt`` may
        be a sequence of absolute x values, one per glyph).
        """
        prec = self.doc.content_precision if precision is None else precision
        gids = list(glyph_ids)
        if per_glyph:
            xs = list(x_pt) if isinstance(x_pt, (list, tuple)) else [
                float(x_pt)] * len(gids)
        else:
            xs = [float(x_pt)] * len(gids)
        block = build_text_run(font.key, font.widths, gids, xs,
                               size_pt, y_pt, x0=float(xs[0]),
                               precision=prec, use_tj=use_tj,
                               per_glyph=per_glyph)
        self._content += block
        return block

    def show_run(self, font, glyph_ids, xs_pt, size_pt, y_pt, *, x0=None,
                 use_tj: bool = True, per_glyph: bool = False,
                 precision: int = None) -> bytes:
        """Kerned run: one BT..ET block placing every glyph at its own x."""
        prec = self.doc.content_precision if precision is None else precision
        block = build_text_run(font.key, font.widths, glyph_ids, xs_pt,
                               size_pt, y_pt, x0=x0, precision=prec,
                               use_tj=use_tj, per_glyph=per_glyph)
        self._content += block
        return block

    # -- graphics ---------------------------------------------------------
    def set_fill_rgb(self, r, g, b) -> None:
        self._content += (self._num(r) + b' ' + self._num(g) + b' '
                          + self._num(b) + b' rg\n')

    def set_stroke_rgb(self, r, g, b) -> None:
        self._content += (self._num(r) + b' ' + self._num(g) + b' '
                          + self._num(b) + b' RG\n')

    def set_fill_gray(self, g) -> None:
        self._content += self._num(g) + b' g\n'

    def rect(self, x, y, w, h) -> None:
        self._content += (b' '.join([self._num(v) for v in (x, y, w, h)])
                          + b' re\n')

    def fill(self) -> None:
        self._content += b'f\n'

    def stroke(self) -> None:
        self._content += b'S\n'

    def fill_stroke(self) -> None:
        self._content += b'B\n'

    def place_xobject(self, key, x, y, wpt, hpt) -> None:
        """``q <sx> 0 0 <sy> <x> <y> cm /<key> Do Q``."""
        self._content += (b'q ' + self._num(wpt) + b' 0 0 ' + self._num(hpt)
                          + b' ' + self._num(x) + b' ' + self._num(y) + b' cm '
                          + pdf_name(key) + b' Do Q\n')

    # -- resources --------------------------------------------------------
    def add_resource(self, kind: str, key: str, ref) -> str:
        self.resources.setdefault(kind, {})[key] = ref
        return key

    def add_image_xobject(self, data, w=None, h=None, *, mode='rgb', name=None,
                          flate_data=None, decode_parms=None, dct_data=None,
                          level=None, smask=None) -> str:
        """Create an image XObject in the page resources; returns its key."""
        return self.doc._add_image_xobject(
            self, data, w, h, mode=mode, name=name, flate_data=flate_data,
            decode_parms=decode_parms, dct_data=dct_data, level=level,
            smask=smask)

    def add_image(self, data, w=None, h=None, x=0.0, y=None, wpt=None,
                  hpt=None, *, y_top=None, mode='rgb', name=None,
                  flate_data=None, decode_parms=None, dct_data=None,
                  level=None, smask=None, place=True) -> str:
        """Create AND place an image XObject; returns its resource key ('Im0').

        ``x``, ``y`` are the PDF user-space coordinates of the LOWER-LEFT
        corner of the placed image (``wpt`` x ``hpt`` in points).  Pass
        ``y_top`` instead of ``y`` to give a top-left-origin y (CAJ style);
        the conversion uses the page height and ``hpt``.
        """
        key = self.add_image_xobject(
            data, w, h, mode=mode, name=name, flate_data=flate_data,
            decode_parms=decode_parms, dct_data=dct_data, level=level,
            smask=smask)
        if place:
            if wpt is None or hpt is None:
                raise ValueError('add_image needs wpt and hpt when placing')
            if y is None:
                if y_top is None:
                    raise ValueError('add_image needs either y or y_top')
                y = self.y_from_top(y_top, hpt)
            self.place_xobject(key, x, y, wpt, hpt)
        return key

    def add_truetype_font(self, name, subset_bytes, code_to_gid, gid_to_adv,
                          upem=2048, metrics=None, *, resource_key=None,
                          base_font=None, subset_tag=None, flags=None,
                          stem_v=None, compress_font=True, level=None,
                          indirect_length=None) -> Font:
        """Register an embedded TrueType subset as a Type0 font on this page."""
        return self.doc._add_truetype_font(
            self, name, subset_bytes, code_to_gid, gid_to_adv, upem, metrics,
            resource_key=resource_key, base_font=base_font,
            subset_tag=subset_tag, flags=flags, stem_v=stem_v,
            compress_font=compress_font, level=level,
            indirect_length=indirect_length)

    # -- serialization ----------------------------------------------------
    def _resource_dict(self) -> dict:
        res = {}
        for kind, mapping in self.resources.items():
            if mapping:
                res[kind] = dict(mapping)
        if self.procset:
            res['ProcSet'] = list(self.procset)
        extra = getattr(self, '_extra_resources', None)
        if extra:
            res.update(extra)
        return res

    def _object_dict(self) -> dict:
        d = {'Type': '/Page', 'Parent': self.doc.pages_ref,
             'MediaBox': list(self.mediabox)}
        if self.rotation:
            d['Rotate'] = int(self.rotation)
        d['Resources'] = self._resource_dict()
        d['Contents'] = self._content_ref
        return d


# --------------------------------------------------------------------------
# document
# --------------------------------------------------------------------------
class PDFDocument:
    """A PDF 1.7 document.  See the module docstring for the value encoding."""

    def __init__(self, title=None, author=None, producer=None, creator=None,
                 subject=None, keywords=None, version=PDF_VERSION_DEFAULT,
                 float_precision: int = 6, content_precision: int = 3,
                 compress_level: int = 9, compress_streams: bool = True,
                 creation_date=None, doc_id=None,
                 indirect_lengths: bool = False):
        self.version = str(version)
        self.float_precision = int(float_precision)
        self.content_precision = int(content_precision)
        self.compress_level = int(compress_level)
        self.compress_streams = bool(compress_streams)
        self.indirect_lengths = bool(indirect_lengths)
        self.title = title
        self.author = author
        self.producer = producer
        self.creator = creator
        self.subject = subject
        self.keywords = keywords
        self.creation_date = creation_date      # None -> omit, 'now' -> stamp
        self.doc_id = doc_id

        self._objs = {}                          # num -> value
        self._next_num = 1
        self._font_serial = 0
        self._image_serial = 0
        self._finalized = False
        self.pages = []
        self.fonts = {}                          # base_font -> Font
        self.offsets = {}
        self.xref_offset = 0
        self.file_size = 0

        self.catalog_ref = self.make_indirect({'Type': '/Catalog'})
        self.pages_ref = self.make_indirect({'Type': '/Pages', 'Kids': [],
                                             'Count': 0})
        self.info_ref = self.make_indirect({})
        self._objs[self.catalog_ref.num]['Pages'] = self.pages_ref

    # -- object table -----------------------------------------------------
    def _alloc(self) -> int:
        num = self._next_num
        self._next_num += 1
        return num

    def make_indirect(self, value) -> Ref:
        """Allocate an object number, store ``value``, return its Ref."""
        num = self._alloc()
        self._objs[num] = value
        return Ref(num, 0)

    def object_count(self) -> int:
        return self._next_num - 1

    # -- pages ------------------------------------------------------------
    def add_page(self, width_pt: float, height_pt: float) -> Page:
        page = Page(self, width_pt, height_pt)
        page.key = 'Pg%d' % (len(self.pages) + 1)
        page.ref = self.make_indirect(None)      # dict filled in at save time
        self.pages.append(page)
        kids = self._objs[self.pages_ref.num]['Kids']
        kids.append(page.ref)
        self._objs[self.pages_ref.num]['Count'] = len(kids)
        return page

    # -- images -----------------------------------------------------------
    def _next_image_key(self) -> str:
        key = 'Im%d' % self._image_serial
        self._image_serial += 1
        return key

    def _image_spec(self, data, w, h, mode, flate_data, decode_parms,
                    dct_data, level, smask):
        """Validate inputs and return (entries, payload_stream_kwargs, w, h)."""
        mode = (mode or 'rgb').lower()
        lvl = self.compress_level if level is None else int(level)

        if mode in ('jpeg', 'dct'):
            payload = dct_data if dct_data is not None else data
            if payload is None:
                raise ValueError('mode=jpeg needs dct_data or data')
            payload = bytes(payload)
            if payload[:2] != b'\xff\xd8':
                raise ValueError('mode=jpeg but the bytes are not a JPEG')
            jw, jh, ncomp = jpeg_sof_info(payload)
            if w is None:
                w = jw
            if h is None:
                h = jh
            if int(w) != int(jw) or int(h) != int(jh):
                raise ValueError('JPEG is %dx%d but w/h say %sx%s'
                                 % (jw, jh, w, h))
            if ncomp == 1:
                cs = '/DeviceGray'
            elif ncomp == 3:
                cs = '/DeviceRGB'
            elif ncomp == 4:
                cs = '/DeviceCMYK'
            else:
                raise ValueError('unsupported JPEG component count %d' % ncomp)
            entries = {'Type': '/XObject', 'Subtype': '/Image', 'Width': int(w),
                       'Height': int(h), 'ColorSpace': cs,
                       'BitsPerComponent': 8}
            if cs == '/DeviceCMYK' and jpeg_is_adobe(payload):
                entries['Decode'] = [1, 0, 1, 0, 1, 0, 1, 0]
            if smask is not None:
                entries['SMask'] = smask
            return entries, dict(data=payload, compress=False,
                                 pre_compressed=True, filter_name='/DCTDecode',
                                 level=lvl), int(w), int(h)

        if mode in ('rgb', 'rgb-raw', 'gray', 'gray-raw', 'flate'):
            colors = 1 if mode.startswith('gray') else 3
            cs = '/DeviceGray' if colors == 1 else '/DeviceRGB'
            raw_mode = mode.endswith('-raw')
            if flate_data is not None:
                payload = bytes(flate_data)
                if raw_mode:
                    raise ValueError('flate_data given with an unfiltered mode')
                if decode_parms is None:
                    if w is None or h is None:
                        raise ValueError('flate_data needs explicit w and h')
                    decode_parms = {'Predictor': 15, 'Colors': colors,
                                    'BitsPerComponent': 8, 'Columns': int(w)}
                entries = {'Type': '/XObject', 'Subtype': '/Image',
                           'Width': int(w), 'Height': int(h),
                           'ColorSpace': cs, 'BitsPerComponent': 8}
                if smask is not None:
                    entries['SMask'] = smask
                return entries, dict(data=payload, compress=False,
                                     pre_compressed=True,
                                     filter_name='/FlateDecode',
                                     decode_parms=decode_parms, level=lvl), \
                    int(w), int(h)
            # caller supplied raw samples (bytes) or a PIL image
            if data is None:
                raise ValueError('mode=%s needs data' % mode)
            if hasattr(data, 'tobytes') and hasattr(data, 'size') \
                    and hasattr(data, 'mode'):
                img = data
                if img.mode == 'L':
                    colors = 1
                    cs = '/DeviceGray'
                elif img.mode == 'RGB':
                    colors = 3
                    cs = '/DeviceRGB'
                else:
                    img = img.convert('RGB' if colors == 3 else 'L')
                iw, ih = img.size
                if w is None:
                    w = iw
                if h is None:
                    h = ih
                if (int(w), int(h)) != (int(iw), int(ih)):
                    raise ValueError('image is %dx%d but w/h say %sx%s'
                                     % (iw, ih, w, h))
                data = img.tobytes()
            w = int(w)
            h = int(h)
            data = bytes(data)
            if len(data) != w * h * colors:
                raise ValueError('raw samples are %d bytes, expected %d*%d*%d'
                                 ' = %d' % (len(data), h, w, colors,
                                            w * h * colors))
            entries = {'Type': '/XObject', 'Subtype': '/Image', 'Width': w,
                       'Height': h, 'ColorSpace': cs, 'BitsPerComponent': 8}
            if smask is not None:
                entries['SMask'] = smask
            if raw_mode:
                return entries, dict(data=data, compress=False, level=lvl), w, h
            filtered, _chosen = png_filter_scanlines(data, w, h, colors)
            parms = (decode_parms if decode_parms is not None else
                     {'Predictor': 15, 'Colors': colors, 'BitsPerComponent': 8,
                      'Columns': w})
            return entries, dict(data=filtered, compress=self.compress_streams,
                                 decode_parms=parms, level=lvl), w, h

        raise ValueError('unknown image mode %r' % (mode,))

    def _add_image_xobject(self, page, data, w, h, *, mode, name, flate_data,
                           decode_parms, dct_data, level, smask) -> str:
        entries, skw, w, h = self._image_spec(
            data, w, h, mode, flate_data, decode_parms, dct_data, level, smask)
        stream = PDFStream(**skw)
        stream.indirect_length = self.indirect_lengths
        ref = self.make_indirect(stream)
        key = name if name is not None else self._next_image_key()
        if key.startswith('/'):
            key = key[1:]
        page.add_resource('XObject', key, ref)
        if '/ImageC' not in page.procset:
            page.procset.append('/ImageC')
        return key

    # -- fonts ------------------------------------------------------------
    def _add_truetype_font(self, page, name, subset_bytes, code_to_gid,
                           gid_to_adv, upem, metrics, *, resource_key,
                           base_font, subset_tag, flags, stem_v, compress_font,
                           level, indirect_length) -> Font:
        subset_bytes = bytes(subset_bytes)
        if not subset_bytes:
            raise ValueError('empty font subset')
        if subset_bytes[:4] != b'\x00\x01\x00\x00' and \
                subset_bytes[:4] != b'true':
            raise ValueError('FontFile2 payload must be a TrueType sfnt '
                             'starting with 0x00010000 (got %s)'
                             % subset_bytes[:4].hex())
        if metrics is None:
            try:
                metrics = parse_ttf_metrics(subset_bytes)
            except Exception:
                metrics = {}
        upem = int(upem or metrics.get('upem') or 1000)
        if upem <= 0:
            raise ValueError('bad upem %r' % (upem,))

        code_to_gid = {int(k): int(v) for k, v in dict(code_to_gid).items()}
        gid_to_adv = {int(k): int(v) for k, v in dict(gid_to_adv).items()}
        widths = {g: int(round(a * 1000.0 / upem)) for g, a in gid_to_adv.items()}
        for g in set(code_to_gid.values()) - set(widths):
            widths[g] = 0
        if not widths:
            raise ValueError('gid_to_adv is empty')
        # /DW: most common width; ties broken by the smaller width
        counts = {}
        for wd in widths.values():
            counts[wd] = counts.get(wd, 0) + 1
        dw = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]

        fam = _sanitize_ps_name(name or 'Font')
        self._font_serial += 1
        tag = subset_tag or _subset_tag(self, fam)
        base_font = base_font or ('%s+%s' % (tag, fam))
        base_font = _sanitize_ps_name(base_font, keep_plus=True)
        if base_font.startswith('/'):
            base_font = base_font[1:]

        scale = 1000.0 / upem

        def s1000(v, default=0):
            if v is None:
                v = default
            return int(round(float(v) * scale))

        bbox = list(metrics.get('bbox') or [0, -250, 1000, 1000])
        if len(bbox) != 4:
            raise ValueError('metrics[bbox] must have 4 entries')
        bbox1000 = [s1000(bbox[0]), s1000(bbox[1]), s1000(bbox[2]),
                    s1000(bbox[3])]
        ascent = s1000(metrics.get('ascent'), 800)
        descent = s1000(metrics.get('descent'), -200)
        cap = metrics.get('capHeight')
        cap1000 = s1000(cap, ascent) if cap else ascent
        xh = metrics.get('xHeight')
        stemv = s1000(stem_v if stem_v is not None else
                      metrics.get('stemV', 80), 80)
        if stemv <= 0:
            stemv = 80
        italic = float(metrics.get('italicAngle', 0.0) or 0.0)
        if flags is None:
            flags = int(metrics.get('flags', 4))
        flags = int(flags)

        gid_to_codes = {}
        for code, gid in code_to_gid.items():
            gid_to_codes.setdefault(gid, []).append(code)
        for gid in gid_to_codes:
            gid_to_codes[gid] = tuple(sorted(gid_to_codes[gid]))
        tounicode_bytes = build_tounicode_cmap(gid_to_codes)

        lvl = self.compress_level if level is None else int(level)
        ind_len = (self.indirect_lengths if indirect_length is None
                   else bool(indirect_length))

        # -- allocate in a fixed, readable order
        font_ref = self.make_indirect(None)
        cidfont_ref = self.make_indirect(None)
        descriptor_ref = self.make_indirect(None)
        file_ref = self.make_indirect(PDFStream(
            subset_bytes,
            entries={'Length1': len(subset_bytes)},
            compress=bool(compress_font) and self.compress_streams,
            level=lvl, indirect_length=ind_len))
        tounicode_ref = self.make_indirect(PDFStream(
            tounicode_bytes, entries={},
            compress=self.compress_streams, level=lvl,
            indirect_length=ind_len))

        w_tokens = compact_w_array(widths, dw)

        self._objs[font_ref.num] = {
            'Type': '/Font', 'Subtype': '/Type0', 'BaseFont': '/' + base_font,
            'Encoding': '/Identity-H', 'DescendantFonts': [cidfont_ref],
            'ToUnicode': tounicode_ref,
        }
        cid = {
            'Type': '/Font', 'Subtype': '/CIDFontType2',
            'BaseFont': '/' + base_font,
            'CIDSystemInfo': {'Registry': b'Adobe', 'Ordering': b'Identity',
                              'Supplement': 0},
            'FontDescriptor': descriptor_ref,
            'DW': int(dw),
            'CIDToGIDMap': '/Identity',
        }
        if w_tokens:
            cid['W'] = [Raw(t) for t in w_tokens]
        self._objs[cidfont_ref.num] = cid
        fd = {
            'Type': '/FontDescriptor', 'FontName': '/' + base_font,
            'Flags': flags, 'FontBBox': bbox1000, 'ItalicAngle': italic,
            'Ascent': ascent, 'Descent': descent, 'CapHeight': cap1000,
            'StemV': stemv, 'FontFile2': file_ref,
        }
        if xh:
            fd['XHeight'] = s1000(xh)
        avg = metrics.get('avgWidth')
        if avg:
            fd['AvgWidth'] = s1000(avg)
        mx = metrics.get('maxWidth')
        if mx:
            fd['MaxWidth'] = s1000(mx)
        self._objs[descriptor_ref.num] = fd

        font = Font(self, resource_key or ('F%d' % len(self.fonts)),
                    base_font, subset_bytes, code_to_gid, gid_to_adv, upem,
                    metrics, (font_ref, cidfont_ref, descriptor_ref, file_ref,
                              tounicode_ref), tounicode_bytes, widths, dw,
                    len(subset_bytes))
        self.fonts[base_font] = font
        page.add_resource('Font', font.key, font_ref)
        if '/Text' not in page.procset:
            page.procset.append('/Text')
        return font

    # -- info -------------------------------------------------------------
    def _pdf_date(self, when=None) -> bytes:
        import time
        if when is None:
            t = time.localtime()
            off = -time.timezone if not t.tm_isdst else -time.altzone
        else:
            t = when.timetuple()
            off = 0
        sign = b'+' if off >= 0 else b'-'
        off = abs(off)
        return b'D:%04d%02d%02d%02d%02d%02d%s%02d\'%02d\'' % (
            t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec,
            sign, off // 3600, (off % 3600) // 60)

    def _info_dict(self) -> dict:
        d = {}
        for key, val in (('Title', self.title), ('Author', self.author),
                         ('Producer', self.producer), ('Creator', self.creator),
                         ('Subject', self.subject), ('Keywords', self.keywords)):
            if val is not None:
                d[key] = Literal(val) if isinstance(val, str) else bytes(val)
        if self.creation_date is not None:
            if isinstance(self.creation_date, bytes):
                d['CreationDate'] = self.creation_date
            elif self.creation_date == 'now':
                d['CreationDate'] = self._pdf_date()
            else:
                d['CreationDate'] = self._pdf_date(self.creation_date)
        return d

    # -- serialization ----------------------------------------------------
    def _finalize(self) -> None:
        """Fill in the deferred dictionaries and allocate remaining objects.

        Idempotent: repeated calls do nothing the second time, so
        ``serialize()`` may be called more than once and always yields the
        same bytes.
        """
        if self._finalized:
            return
        self._finalized = True
        self._objs[self.info_ref.num] = self._info_dict()
        pages = self._objs[self.pages_ref.num]
        pages['Kids'] = [p.ref for p in self.pages]
        pages['Count'] = len(self.pages)
        for page in self.pages:
            payload = bytes(page._content)
            stream = PDFStream(payload, entries={},
                               compress=self.compress_streams,
                               level=self.compress_level,
                               indirect_length=self.indirect_lengths)
            page._content_ref = self.make_indirect(stream)
            self._objs[page.ref.num] = page._object_dict()

    def _prepare_streams(self) -> None:
        """Materialize stream payloads; allocate indirect /Length objects."""
        for num in sorted(self._objs):
            val = self._objs[num]
            if isinstance(val, PDFStream):
                val.encoded_bytes()
                if val.indirect_length and val._length_ref is None:
                    ln = self._alloc()
                    self._objs[ln] = len(val.encoded_bytes())
                    val._length_ref = (ln, 0)

    def serialize(self, *, compress_level=None) -> bytes:
        """Build the complete file and return it as bytes."""
        if compress_level is not None:
            self.compress_level = int(compress_level)
        self._finalize()
        self._prepare_streams()

        buf = bytearray()
        buf += ('%%PDF-%s\n' % self.version).encode('ascii')
        # binary comment: 4 bytes all >= 128, so binary-safe viewers see it
        buf += b'%\xe2\xe3\xcf\xd3\n'

        self.offsets = {}
        nums = sorted(self._objs)
        for num in nums:
            if num not in self.offsets:
                self.offsets[num] = len(buf)
            val = self._objs[num]
            buf += ('%d 0 obj\n' % num).encode('ascii')
            if isinstance(val, PDFStream):
                payload = val.encoded_bytes()
                buf += pdf_encode(val.full_dict(self.float_precision),
                                  self.float_precision)
                buf += b'\nstream\n'
                buf += payload
                buf += b'\nendstream\n'
            else:
                buf += pdf_encode(val, self.float_precision)
                buf += b'\n'
            buf += b'endobj\n'

        size = self._next_num                       # == max obj num + 1
        present = set(nums)
        # free-entry linked list: object 0 points at the lowest free num,
        # each free entry points at the next one, the last points at 0.
        free_nums = sorted(n for n in range(1, size) if n not in present)
        nxt = {}
        for i, n in enumerate(free_nums):
            nxt[n] = free_nums[i + 1] if i + 1 < len(free_nums) else 0

        xref_off = len(buf)
        self.xref_offset = xref_off
        buf += b'xref\n'
        buf += ('0 %d\n' % size).encode('ascii')
        head_next = free_nums[0] if free_nums else 0
        buf += ('%010d %05d f \n' % (head_next, 65535)).encode('ascii')
        for num in range(1, size):
            if num in present:
                buf += ('%010d %05d n \n' % (self.offsets[num], 0)).encode(
                    'ascii')
            else:
                buf += ('%010d %05d f \n' % (nxt[num], 0)).encode('ascii')

        buf += b'trailer\n'
        trailer = {'Size': size, 'Root': self.catalog_ref,
                   'Info': self.info_ref}
        if self.doc_id is not None:
            did = bytes(self.doc_id)
            trailer['ID'] = [HexStr(did), HexStr(did)]
        buf += pdf_encode(trailer, self.float_precision)
        buf += b'\nstartxref\n'
        buf += ('%d\n' % xref_off).encode('ascii')
        buf += b'%%EOF\n'

        self.file_size = len(buf)
        return bytes(buf)

    def save(self, path_or_fileobj, *, compress_level=None) -> int:
        """Serialize and write to a path (str/os.PathLike) or a binary fileobj.

        Returns the number of bytes written (informational; the documented
        contract is ``-> None``, so ignoring it is fine).
        """
        data = self.serialize(compress_level=compress_level)
        if isinstance(path_or_fileobj, (str, bytes, os.PathLike)):
            with open(path_or_fileobj, 'wb') as fh:
                fh.write(data)
        else:
            path_or_fileobj.write(data)
            try:
                path_or_fileobj.flush()
            except Exception:
                pass
        return len(data)
