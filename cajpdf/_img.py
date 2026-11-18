"""
wfimg_encoder -- the raster path for caj2pdf: turn libreaderex_x64.so page bitmaps
into the most size/quality-efficient PDF image XObject stream, hand-rolled from the
stdlib (+ PIL/numpy).  No pikepdf/reportlab/img2pdf/fitz anywhere.

------------------------------------------------------------------------------
WHAT THE MEASUREMENTS SAID (this file's defaults follow from them)
------------------------------------------------------------------------------
The test document's pages come back from CAJFILE_DrawPageSlice1 as BGR24 with
R==G==B on *every* pixel (measured max channel spread = 0 on all 4 pages at
100/150/200/300 dpi), i.e. they are pure grayscale anti-aliased text.  Two
consequences:

  * DeviceGray instead of DeviceRGB is free quality-wise and roughly halves the
    stream (150 dpi p1: 22.1 KB gray vs 45.5 KB rgb, both lossless).
  * The PNG predictor *hurts* on this content.  min-abs-sum row filtering
    (what PIL/PNG do) picks mostly Up/Paeth, which destroys the long runs of
    identical bytes that deflate loves on text pages:

        150 dpi p1, DeviceGray, zlib 9:
          raw samples, no predictor, Z_FILTERED  16 834 B   <-- best lossless
          raw samples, no predictor, default     17 097 B
          predictor filter 0 (None) + zlib 9     19 033 B
          predictor min-abs-sum adaptive         22 052 B
          PIL PNG IDAT (gray, optimize=True)     21 835 B
          my predictor stream, gray              22 120 B

    So `png_predictor_flate()` is implemented exactly as specified (all five
    filter types, Paeth included, verified round-trip-exact) but the encoder
    *also* tries a plain no-predictor flate and keeps whichever is smaller.
    On photographic/gradient content the predictor wins big (jpeg-decoded
    photo 400x400: 59 895 B predictor vs 98 783 B raw), so this is a genuine
    per-page choice, not a constant.

  * zlib level matters a lot on text (150 dpi gray: L1 24 939 -> L6 17 913 ->
    L9 17 102 -> L9+Z_FILTERED 16 834); Z_FILTERED is ~1.5% smaller than
    default for ~10% less time.  Level 9 is the right default.
  * PIL's `optimize=True` PNG is *worse* than my own stream here, not better
    (21 835 vs my best 16 834), because PNG always carries the predictor and
    PIL's optimize only tunes the zlib side.  Stated plainly: for CAJ text
    pages, do NOT emit PNG-style predictor streams.

------------------------------------------------------------------------------
API
------------------------------------------------------------------------------
encode_image_for_pdf(img_or_bytes, w=None, h=None, *, quality=85, mode='auto',
                     grayscale_thresh=0, raw_mode='BGR', stride=None,
                     zlib_level=9, flate='auto', auto_color_thresh=4096) -> dict

    keys: kind ('flate'|'dct'|'ccitt'), data (bytes), filter_name ('/FlateDecode'
    | '/DCTDecode' | '/CCITTFaxDecode'), decode_parms (dict|None), colorspace
    ('DeviceRGB'|'DeviceGray'), width, height, bits (8|4|2|1), size (=len(data)),
    lossless (bool), gray (bool), spread (int), near_gray (bool), mode (str),
    seconds (float), raw_size (int), + 'flate_method' / 'ncolors' where relevant.

png_predictor_flate(rgb_bytes, w, h, colors, level=9) -> bytes
    PNG per-row filtering (types 0..4, min-|sum| heuristic, spec Paeth) + deflate.
    Pair with flate_predictor_decode_parms(w, colors).

raw_flate(sample_bytes, level=9, strategy=zlib.Z_FILTERED) -> bytes
    Plain /FlateDecode with NO DecodeParms -- the winner on CAJ text pages.

to_gray_if_saving(img, thresh, verify=False, level=9) -> PIL image ('L' or unchanged)

measure(img, qualities=(60,70,80,85,90,95), modes=('flate_gray','flate_rgb','dct'),
        reference=None, ...) -> list[dict]   (size + objective quality numbers)

error_metrics(ref_img, dec_img) -> dict     (max/mean abs error, %px>8, %px>16,
                                             dark-pixel %, stroke symmetric-diff %)
channel_spread(img) -> int
inflate_predictor_stream(data, w, h, colors) -> bytes     (reference decoder)
flate_predictor_decode_parms(w, colors) -> dict
pdf_image_dict_str(enc, objnum=None) -> str               (convenience for the writer)
"""

from __future__ import annotations

import io
import time
import zlib

import numpy as np
from PIL import Image

__all__ = [
    "encode_image_for_pdf",
    "png_predictor_flate",
    "raw_flate",
    "to_gray_if_saving",
    "measure",
    "error_metrics",
    "channel_spread",
    "inflate_predictor_stream",
    "flate_predictor_decode_parms",
    "png_filtered_rows",
    "pdf_image_dict_str",
    "decode_stream_to_image",
    "pack_lowbits",
    "unpack_lowbits",
    "quantize_levels",
    "dequantize_levels",
    "ccitt_g4_data",
]


# --------------------------------------------------------------------------- #
# utilities
# --------------------------------------------------------------------------- #
def channel_spread(img) -> int:
    """max over all pixels of (max channel - min channel); 0 for grey/1-band images.

    Objective near-gray test.  CAJ text pages measure exactly 0 (R==G==B), so
    grayscale_thresh=0 is a *safe* default: nothing is ever discarded by it.
    """
    if not isinstance(img, Image.Image):
        raise TypeError("channel_spread needs a PIL Image")
    if img.mode not in ("RGB", "RGBA", "RGBX", "CMYK", "YCbCr"):
        return 0
    a = np.asarray(img.convert("RGB"), dtype=np.uint8)
    if a.size == 0:
        return 0
    mx = a.max(axis=2).astype(np.int32)
    mn = a.min(axis=2).astype(np.int32)
    return int((mx - mn).max())


def _as_pil(img_or_bytes, w, h, raw_mode="BGR", stride=None):
    """Accept a PIL Image, or the raw bytes from ctypes.string_at(buf, row*h)."""
    if isinstance(img_or_bytes, Image.Image):
        return img_or_bytes
    if isinstance(img_or_bytes, (bytes, bytearray, memoryview)):
        data = bytes(img_or_bytes)
    else:
        raise TypeError("img_or_bytes must be a PIL Image or raw bytes "
                        "(ctypes.string_at(buf, row*h))")
    if w is None or h is None:
        raise ValueError("w and h are required when passing raw bytes")
    w, h = int(w), int(h)
    if stride is None:
        stride = w * 3
    return Image.frombytes("RGB", (w, h), data, "raw", raw_mode, stride)


def _normalize(img):
    """Coerce any PIL mode to 'RGB' or 'L', preserving appearance.
    Alpha is composited onto white (PDF has no page background to inherit)."""
    if img.mode in ("RGB", "L"):
        return img
    if img.mode in ("RGBA", "LA", "RGBa", "La"):
        base = img.convert("RGBA")
        bg = Image.new("RGBA", base.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, base).convert("RGB")
    return img.convert("RGB")


# --------------------------------------------------------------------------- #
# deflate helpers
# --------------------------------------------------------------------------- #
def raw_flate(sample_bytes, level=9, strategy=zlib.Z_FILTERED):
    """Plain zlib stream (PDF /FlateDecode, NO DecodeParms) over the raw samples.
    Z_FILTERED beats Z_DEFAULT_STRATEGY by ~1.5% on text pages and is faster."""
    if isinstance(sample_bytes, Image.Image):
        sample_bytes = sample_bytes.tobytes()
    co = zlib.compressobj(level, zlib.DEFLATED, 15, 9, strategy)
    return co.compress(bytes(sample_bytes)) + co.flush()


# --------------------------------------------------------------------------- #
# PNG row filters (spec-correct, numpy-vectorised)
# --------------------------------------------------------------------------- #
def _paeth_predictor(a, b, c):
    """Standard PNG Paeth predictor, vectorised. a=left, b=above, c=above-left.
    int32 so p = a+b-c cannot overflow; ties resolved in the spec's a,b,c order."""
    a = a.astype(np.int32)
    b = b.astype(np.int32)
    c = c.astype(np.int32)
    p = a + b - c
    pa = np.abs(p - a)
    pb = np.abs(p - b)
    pc = np.abs(p - c)
    return np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c)).astype(np.uint8)


def png_filtered_rows(buf, w, int_h, colors):
    """Return (PNG-style filtered scanline stream, per-filter-type row histogram).

    One filter-type byte + colors*w filtered bytes per row.  Filter choice is the
    heuristic the PNG spec recommends: minimise the sum of the *absolute values of
    the filtered bytes read as signed*.  All five types are implemented and are
    actually chosen on real data (histogram returned for inspection):
        0 None, 1 Sub, 2 Up, 3 Average, 4 Paeth.
    """
    h = int(int_h)
    bpp = int(colors)              # bytes per complete pixel at 8 bpc
    rowlen = int(w) * bpp
    if len(buf) != rowlen * h:
        raise ValueError(f"buffer is {len(buf)} bytes, expected {rowlen*h} ({w}x{h}x{colors})")
    arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape(h, rowlen)
    out = bytearray()
    prev = np.zeros(rowlen, dtype=np.uint8)
    zero_bpp = np.zeros(bpp, dtype=np.uint8)
    zero_row = np.zeros(rowlen, dtype=np.uint8)
    hist = [0, 0, 0, 0, 0]
    for y in range(h):
        cur = arr[y]
        if rowlen > bpp:
            left = np.concatenate((zero_bpp, cur[:-bpp]))
            upleft = np.concatenate((zero_bpp, prev[:-bpp]))
        else:
            # Every byte of a narrower-than-one-pixel row is a row-initial byte:
            # the PNG spec defines the left/up-left neighbours as zero there.
            left = zero_row
            upleft = zero_row
        cands = (
            cur,                                                                     # 0
            (cur - left).astype(np.uint8),                                           # 1
            (cur - prev).astype(np.uint8),                                           # 2
            (cur.astype(np.uint16) - ((left.astype(np.uint16) + prev.astype(np.uint16)) >> 1)).astype(np.uint8),  # 3
            (cur.astype(np.int16) - _paeth_predictor(left, prev, upleft).astype(np.int16)).astype(np.uint8),      # 4
        )
        best_t, best_sum, best_row = 0, None, None
        for t, f in enumerate(cands):
            signed = np.where(f > 127, f.astype(np.int32) - 256, f.astype(np.int32))
            s = int(np.abs(signed).sum())
            if best_sum is None or s < best_sum:
                best_sum, best_t, best_row = s, t, f
        hist[best_t] += 1
        out.append(best_t)
        out += best_row.tobytes()
        prev = cur
    return bytes(out), hist


def flate_predictor_decode_parms(w, colors) -> dict:
    """DecodeParms for a Predictor-15 (PNG optimum) stream."""
    return {"Predictor": 15, "Colors": int(colors), "BitsPerComponent": 8, "Columns": int(w)}


def png_predictor_flate(rgb_bytes, w, h, colors, level=9) -> bytes:
    """Per-row PNG filtering + zlib deflate.  `rgb_bytes` = h rows of colors*w
    8-bit samples (colors==3 RGB, 1 DeviceGray).  Pair with
    flate_predictor_decode_parms(w, colors) i.e. /Predictor 15.

    NOTE: on CAJ text pages this is *larger* than raw_flate() -- measured, see the
    module docstring.  Keep it for photographic content; the encoder picks per page.
    """
    w, h, colors = int(w), int(h), int(colors)
    if isinstance(rgb_bytes, Image.Image):
        rgb_bytes = rgb_bytes.tobytes()
    filtered, _ = png_filtered_rows(bytes(rgb_bytes), w, h, colors)
    return zlib.compress(filtered, level)


def inflate_predictor_stream(data: bytes, w: int, h: int, colors: int) -> bytes:
    """Reference *un-filter* (PNG spec 9.3), pure python, no numpy.
    /tmp/wfimg_test.py deliberately carries its own independent copy of this so
    the round trip is checked by two separate implementations."""
    raw = zlib.decompress(data)
    bpp = int(colors)
    rowlen = int(w) * bpp
    out = bytearray()
    prev = bytearray(rowlen)
    pos = 0
    for _ in range(int(h)):
        ft = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos + rowlen]); pos += rowlen
        if len(line) != rowlen:
            raise ValueError("truncated predictor stream")
        if ft == 0:
            pass
        elif ft == 1:
            for i in range(bpp, rowlen):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(rowlen):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(rowlen):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(rowlen):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        else:
            raise ValueError(f"bad filter type {ft}")
        out += line
        prev = line
    return bytes(out)


# --------------------------------------------------------------------------- #
# gray conversion
# --------------------------------------------------------------------------- #
def to_gray_if_saving(img, thresh, verify=False, level=9):
    """Convert to mode 'L' when (a) the image is near-gray -- max channel spread
    <= thresh -- and (b) doing so actually saves bytes.  Returns `img` unchanged
    otherwise (never returns a lossy conversion for a genuinely coloured page).

    thresh semantics (objective): 0 -> only *exactly* gray images (R==G==B on
    every pixel) -- the CAJ case, spread measured 0 on all four test pages.
    >0 -> tolerate up to `thresh` of per-pixel channel spread (JPEG ringing,
    colour fringing, scanned pages).  Every 1 of spread costs 3x the samples.

    verify=False relies on the structural argument (a gray row is 1/3 of the
    samples of an RGB row through the identical filter+deflate pipeline, and
    deflate cost is monotone in input length for these streams) and is what the
    encoder uses; verify=True compresses both ways and compares, for callers who
    want the proof.
    """
    img = _normalize(img)
    if img.mode == "L":
        return img
    if channel_spread(img) > thresh:
        return img
    gray = img.convert("L")
    if not verify:
        return gray
    w, h = img.size
    rgb_len = min(len(raw_flate(img.tobytes(), level)),
                  len(png_predictor_flate(img.tobytes(), w, h, 3, level)))
    gray_len = min(len(raw_flate(gray.tobytes(), level)),
                   len(png_predictor_flate(gray.tobytes(), w, h, 1, level)))
    return gray if gray_len < rgb_len else img


# --------------------------------------------------------------------------- #
# the three stream encoders
# --------------------------------------------------------------------------- #
def _encode_flate(img, level=9, flate="auto"):
    """Lossless flate.  flate='auto' tries both the plain (no-predictor) stream and
    the PNG-predictor stream and keeps the smaller; 'raw' / 'predictor' force one."""
    img = _normalize(img)
    w, h = img.size
    colors = 1 if img.mode == "L" else 3
    samples = img.tobytes()
    t0 = time.perf_counter()

    cands = []
    if flate in ("auto", "raw"):
        d = raw_flate(samples, level)
        cands.append((len(d), d, None, "raw"))
    if flate in ("auto", "predictor"):
        d = png_predictor_flate(samples, w, h, colors, level)
        cands.append((len(d), d, flate_predictor_decode_parms(w, colors), "predictor"))
    if not cands:
        raise ValueError(f"unknown flate mode {flate!r}")
    size, data, parms, method = min(cands, key=lambda c: c[0])
    dt = time.perf_counter() - t0
    return {
        "kind": "flate",
        "data": data,
        "filter_name": "/FlateDecode",
        "decode_parms": parms,
        "colorspace": "DeviceGray" if colors == 1 else "DeviceRGB",
        "width": w, "height": h, "bits": 8,
        "size": len(data), "lossless": True, "gray": colors == 1,
        "raw_size": w * h * colors, "seconds": dt, "flate_method": method,
    }


def _encode_dct(img, quality=85, optimize=True, subsampling=0):
    """JPEG (PDF /DCTDecode).  subsampling=0 -> 4:4:4, i.e. no chroma decimation:
    on text this is strictly better than PIL's default 4:2:0 at the same quality
    (measured 150 dpi p1 q85: 4:4:4 rgb 53 474 B vs 4:2:0 rgb 45 444 B, and the
    *gray* 4:4:4 variant 42 195 B beats both while keeping full luma detail)."""
    img = _normalize(img)
    w, h = img.size
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=int(quality), optimize=bool(optimize),
             subsampling=int(subsampling), progressive=False)
    data = buf.getvalue()
    return {
        "kind": "dct",
        "data": data,
        "filter_name": "/DCTDecode",
        "decode_parms": None,
        "colorspace": "DeviceGray" if img.mode == "L" else "DeviceRGB",
        "width": w, "height": h, "bits": 8,
        "size": len(data), "lossless": False, "gray": img.mode == "L",
        "raw_size": w * h * (1 if img.mode == "L" else 3), "seconds": None,
        "quality": int(quality), "subsampling": int(subsampling),
    }


def pack_lowbits(a, bits):
    """Pack a uint8 array of values in 0..(2**bits - 1) into PDF's MSB-first,
    right-zero-padded, row-aligned bit layout.  Returns (packed bytes, rowbytes)."""
    h, w = a.shape
    levels = (1 << bits) - 1
    if int(a.max(initial=0)) > levels:
        raise ValueError(f"values exceed {bits} bits")
    per_row = bits * w
    pad = (-per_row) % 8
    rowbytes = (per_row + pad) // 8
    shifts = np.arange(bits - 1, -1, -1, dtype=np.uint8)
    bitplane = ((a[:, :, None] >> shifts) & 1).astype(np.uint8).reshape(h, per_row)
    if pad:
        bitplane = np.concatenate((bitplane, np.zeros((h, pad), np.uint8)), axis=1)
    return np.packbits(bitplane, axis=1).tobytes(), rowbytes


def unpack_lowbits(raw, w, h, bits):
    """Inverse of pack_lowbits: returns the stored values (0..2**bits-1) as uint8."""
    per_row = bits * w
    pad = (-per_row) % 8
    rowbytes = (per_row + pad) // 8
    bp = np.unpackbits(np.frombuffer(raw, np.uint8).reshape(h, rowbytes), axis=1)[:, :per_row]
    shifts = np.arange(bits - 1, -1, -1, dtype=np.uint16)
    return (bp.reshape(h, w, bits).astype(np.uint16) * shifts_pow(bits)).sum(axis=2).astype(np.uint8)


def shifts_pow(bits):
    return (1 << np.arange(bits - 1, -1, -1)).astype(np.uint16)


def quantize_levels(a, bits):
    """Map 0..255 samples onto the nearest of the 2**bits device levels (round-half-up),
    matching how a PDF reader rescales an n-bit sample: level*(255/(2**bits-1))."""
    levels = (1 << bits) - 1
    return ((a.astype(np.uint16) * levels + 127) // 255).astype(np.uint8)


def dequantize_levels(q, bits):
    """What a PDF viewer will display for those n-bit levels, back on a 0..255 scale."""
    levels = (1 << bits) - 1
    return ((q.astype(np.uint16) * 255 + levels // 2) // levels).astype(np.uint8)


def _encode_lowbits(img, bits=1, level=9):
    """Bit-depth reduction: quantise 'L' onto 2**bits levels and emit a plain
    /FlateDecode stream with /BitsPerComponent bits (no predictor, no DecodeParms
    beyond BitsPerComponent).  LOSSY for bits<8 but dramatically smaller on
    black-on-white text (150 dpi p1: 8-bit 16 834 B -> 4-bit 6 938 B -> 2-bit
    3 693 B -> 1-bit 2 480 B, all raw flate)."""
    img = _normalize(img)
    if img.mode != "L":
        img = img.convert("L")
    w, h = img.size
    if bits not in (1, 2, 4, 8):
        raise ValueError("bits must be 1, 2, 4 or 8")
    if bits == 8:
        return _encode_flate(img, level, "raw")
    q = quantize_levels(np.asarray(img, dtype=np.uint8), bits)
    t0 = time.perf_counter()
    packed, rowbytes = pack_lowbits(q, bits)
    data = raw_flate(packed, level)
    dt = time.perf_counter() - t0
    return {
        "kind": "flate",
        "data": data,
        "filter_name": "/FlateDecode",
        "decode_parms": None,
        "colorspace": "DeviceGray",
        "width": w, "height": h, "bits": bits,
        "size": len(data), "lossless": False, "gray": True,
        "raw_size": rowbytes * h, "seconds": dt, "flate_method": "raw",
        "packed_size": rowbytes * h,
    }


# --------------------------------------------------------------------------- #
# main entry point
# --------------------------------------------------------------------------- #
_FLATE_MODES = {"flate", "flate_gray", "flate_rgb"}
_DCT_MODES = {"dct", "jpeg", "dct_gray", "jpeg_gray", "dct_rgb", "jpeg_rgb"}
_LOWBIT_MODES = {"lowbits", "bilevel", "bilevel1", "gray4", "gray2"}


def encode_image_for_pdf(img_or_bytes, w=None, h=None, *, quality=85, mode="auto",
                         grayscale_thresh=0, raw_mode="BGR", stride=None,
                         zlib_level=9, flate="auto", auto_color_thresh=4096,
                         bits=1, subsampling=0, optimize_jpeg=True):
    """Encode one page bitmap as a PDF image XObject stream.

    img_or_bytes  : PIL Image, or the raw bytes from
                    ctypes.string_at(buf, row*h) straight out of
                    CAJFILE_DrawPageSlice1.
    w, h          : required when img_or_bytes is raw bytes.
    raw_mode      : 'BGR' (what the library returns) or 'RGB'.
    stride        : row stride in bytes; the library returns w*3 (verified:
                    957x1481@150dpi -> row=2872, 1915x2963@300dpi -> row=5748).
    mode          : 'auto'          -- lossless flate, DeviceGray when the page is
                                       near-gray (CAJ pages are), unless the page is
                                       photographic (> auto_color_thresh distinct
                                       colours) in which case DCT.
                    'flate'         -- lossless, gray if near-gray (see to_gray_if_saving)
                    'flate_gray'    -- lossless DeviceGray (forced)
                    'flate_rgb'     -- lossless DeviceRGB (forced)
                    'dct'           -- JPEG q=`quality`, DeviceGray if near-gray
                    'dct_rgb'/'dct_gray' -- JPEG, forced colourspace
                    'bilevel'       -- 1-bit DeviceGray raw flate (LOSSY, ~4x smaller
                                       than 8-bit on text)
                    'lowbits'       -- `bits`-bit DeviceGray raw flate (LOSSY)
    quality       : JPEG quality (dct modes only).
    grayscale_thresh : max tolerated per-pixel channel spread for the gray path.
    zlib_level    : deflate level for flate (9 = best; 6 saves ~40% of the time
                    for ~4% more bytes).
    flate         : 'auto' (try no-predictor and PNG-predictor, keep the smaller),
                    'raw' or 'predictor'.
    subsampling   : JPEG chroma subsampling, 0 = 4:4:4 (recommended for text).
    """
    t0 = time.perf_counter()
    img = _normalize(_as_pil(img_or_bytes, w, h, raw_mode, stride))
    spread = channel_spread(img)
    near_gray = (img.mode == "L") or (spread <= grayscale_thresh)
    m = str(mode).lower()

    if m in _FLATE_MODES:
        if m == "flate_gray":
            img = img.convert("L")
        elif m == "flate_rgb":
            img = img.convert("RGB")
        elif m == "flate":
            img = to_gray_if_saving(img, grayscale_thresh)
        out = _encode_flate(img, zlib_level, flate)
        out["ncolors"] = None
    elif m in _DCT_MODES:
        if m in ("dct_gray", "jpeg_gray"):
            img = img.convert("L")
        elif m in ("dct_rgb", "jpeg_rgb"):
            img = img.convert("RGB")
        elif near_gray:
            img = img.convert("L")
        out = _encode_dct(img, quality, optimize_jpeg, subsampling)
        out["ncolors"] = None
    elif m == "bilevel":
        out = _encode_lowbits(img, 1, zlib_level)
    elif m == "lowbits":
        out = _encode_lowbits(img, int(bits), zlib_level)
    elif m in ("auto", "best"):
        photographic = False
        ncols = None
        if auto_color_thresh is not None:
            a = np.asarray(img.convert("RGB")).reshape(-1, 3).astype(np.uint32)
            ncols = int(len(np.unique((a[:, 0] << 16) | (a[:, 1] << 8) | a[:, 2])))
            photographic = ncols > auto_color_thresh
        base = img.convert("L") if near_gray else img
        out = (_encode_dct(base, quality, optimize_jpeg, subsampling) if photographic
               else _encode_flate(base, zlib_level, flate))
        out["ncolors"] = ncols
        out["photographic"] = photographic
    else:
        raise ValueError(f"unknown mode {mode!r}")

    out["spread"] = int(spread)
    out["near_gray"] = bool(near_gray)
    out["mode"] = m
    out.setdefault("quality", quality if out["kind"] == "dct" else None)
    out["seconds"] = (out.get("seconds") or 0.0) + (time.perf_counter() - t0)
    return out


def pdf_image_dict_str(enc, objnum=None):
    """Convenience for the writer: the PDF dictionary text for an encoded stream
    (the orchestrator owns object/xref bookkeeping)."""
    dp = enc.get("decode_parms")
    dpstr = ""
    if dp:
        dpstr = " /DecodeParms << " + " ".join(f"/{k} {v}" for k, v in dp.items()) + " >>"
    head = (f"<< /Type /XObject /Subtype /Image /Width {enc['width']} "
            f"/Height {enc['height']} /ColorSpace /{enc['colorspace']} "
            f"/BitsPerComponent {enc['bits']} /Filter {enc['filter_name']}{dpstr} "
            f"/Length {enc['size']} >>")
    if objnum is None:
        return head
    return f"{objnum} 0 obj\n{head}\nstream\n"


def decode_stream_to_image(enc):
    """Invert an encode_image_for_pdf() result back to a PIL 'RGB'/'L' image.
    Handles every kind the encoder can emit (flate 8-bit, flate with a PNG
    predictor, flate at 1/2/4 bits, and JPEG)."""
    w, h, bits = enc["width"], enc["height"], enc.get("bits", 8)
    if enc["kind"] == "dct":
        return Image.open(io.BytesIO(enc["data"])).convert("RGB")
    dp = enc.get("decode_parms") or {}
    if dp.get("Predictor") == 15:
        colors = 1 if enc["colorspace"] == "DeviceGray" else 3
        raw = inflate_predictor_stream(enc["data"], w, h, colors)
        return Image.frombytes("L" if colors == 1 else "RGB", (w, h), raw)
    raw = zlib.decompress(enc["data"])
    if bits == 8:
        mode = "L" if enc["colorspace"] == "DeviceGray" else "RGB"
        return Image.frombytes(mode, (w, h), raw)
    q = unpack_lowbits(raw, w, h, bits)
    return Image.fromarray(dequantize_levels(q, bits), "L")


# --------------------------------------------------------------------------- #
# objective quality metrics
# --------------------------------------------------------------------------- #
def error_metrics(ref_img, dec_img, dark_thresh=128):
    """Objective quality numbers of `dec_img` versus the lossless reference.

    max_abs / mean_abs   : per-channel absolute error over the RGB cube
    pct_gt8 / pct_gt16   : % of channel *samples* with error > 8 / > 16
    px_gt8_pct/px_gt16_pct: % of *pixels* whose max channel error > 8 / > 16
    dark_ref_pct/dark_dec_pct : % of pixels below `dark_thresh` in each
    stroke_symdiff_pct   : symmetric difference of the two binarised masks as a %
                           of their union.  THIS is the number that makes text look
                           fuzzy or broken; 0.000 == identical stroke geometry.
    stroke_symdiff_px / stroke_union_px : the same as raw counts.
    """
    ref = np.asarray(_normalize(ref_img).convert("RGB"), dtype=np.int16)
    dec = np.asarray(_normalize(dec_img).convert("RGB"), dtype=np.int16)
    if ref.shape != dec.shape:
        raise ValueError(f"shape mismatch {ref.shape} vs {dec.shape}")
    err = np.abs(ref - dec)
    per_px = err.max(axis=2)
    rl = np.asarray(_normalize(ref_img).convert("L"))
    dl = np.asarray(_normalize(dec_img).convert("L"))
    rb = rl < dark_thresh
    db = dl < dark_thresh
    sym = np.logical_xor(rb, db)
    union = np.logical_or(rb, db)
    n = err.size
    return {
        "max_abs": int(err.max()) if n else 0,
        "mean_abs": float(err.mean()) if n else 0.0,
        "pct_gt8": float((err > 8).mean() * 100.0) if n else 0.0,
        "pct_gt16": float((err > 16).mean() * 100.0) if n else 0.0,
        "px_gt8_pct": float((per_px > 8).mean() * 100.0) if n else 0.0,
        "px_gt16_pct": float((per_px > 16).mean() * 100.0) if n else 0.0,
        "dark_ref_pct": float(rb.mean() * 100.0) if n else 0.0,
        "dark_dec_pct": float(db.mean() * 100.0) if n else 0.0,
        "stroke_symdiff_pct": float(sym.sum() / union.sum() * 100.0) if union.any() else 0.0,
        "stroke_symdiff_px": int(sym.sum()),
        "stroke_union_px": int(union.sum()),
    }


def measure(img, qualities=(60, 70, 80, 85, 90, 95),
            modes=("flate_gray", "flate_rgb", "dct"), reference=None,
            zlib_level=9, gray_thresh=0, dct_gray=True, metrics=True,
            flate="auto", lowbits=(4, 2, 1), subsampling=0):
    """Size + objective-quality sweep.  One dict per (mode, quality) with keys:
    mode, kind, quality, colorspace, bits, width, height, size, kb, seconds,
    lossless, flate_method, and (when metrics=True) every key of error_metrics().
    """
    img = _normalize(img)
    ref = _normalize(reference) if reference is not None else img
    rows = []

    def add(mode, q, enc, bits=None):
        row = {
            "mode": mode, "kind": enc["kind"], "quality": q,
            "colorspace": enc["colorspace"], "bits": enc.get("bits", 8),
            "width": enc["width"], "height": enc["height"],
            "size": enc["size"], "kb": enc["size"] / 1024.0,
            "seconds": enc.get("seconds"), "lossless": enc["lossless"],
            "flate_method": enc.get("flate_method"),
        }
        if metrics:
            row.update(error_metrics(ref, decode_stream_to_image(enc)))
        rows.append(row)

    for mode in modes:
        if mode in ("dct", "dct_gray", "dct_rgb", "jpeg"):
            variants = ["dct_rgb"] if mode == "dct_rgb" else (["dct_gray"] if mode == "dct_gray" else ["dct"])
            if mode == "dct" and dct_gray:
                variants = ["dct", "dct_gray"]
            for variant in variants:
                for q in qualities:
                    t0 = time.perf_counter()
                    enc = encode_image_for_pdf(img, mode=variant, quality=q,
                                               grayscale_thresh=gray_thresh,
                                               subsampling=subsampling)
                    enc["seconds"] = time.perf_counter() - t0
                    add(variant, q, enc)
        elif mode in _FLATE_MODES:
            t0 = time.perf_counter()
            enc = encode_image_for_pdf(img, mode=mode, grayscale_thresh=gray_thresh,
                                       zlib_level=zlib_level, flate=flate)
            enc["seconds"] = time.perf_counter() - t0
            add(mode, None, enc)
        elif mode in ("lowbits", "bilevel"):
            for b in (1,) if mode == "bilevel" else lowbits:
                t0 = time.perf_counter()
                enc = encode_image_for_pdf(img, mode="lowbits", bits=b,
                                           grayscale_thresh=gray_thresh,
                                           zlib_level=zlib_level)
                enc["seconds"] = time.perf_counter() - t0
                add(f"lowbits{b}", None, enc)
        else:
            raise ValueError(f"measure: unknown mode {mode!r}")
    return rows
