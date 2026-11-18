"""TrueType subsetting: keep only the glyphs a document uses.

Pure stdlib. There is no fontTools here, so the sfnt surgery is done by hand:

  * parse the source cmap (formats 12/4/6/0/2) for codepoint -> gid,
  * walk `glyf` and close over COMPOSITE component references, so a glyph built
    from other glyphs does not arrive with missing pieces,
  * renumber gids densely (.notdef first, then ascending old-gid order) and
    rewrite every composite component index to the new numbering,
  * rebuild loca (long or short, whichever fits), hmtx (honouring that
    numberOfHMetrics may be < numGlyphs in the source), maxp.numGlyphs,
    hhea.numberOfHMetrics, head.indexToLocFormat and checkSumAdjustment,
  * write a fresh cmap that maps the requested codepoints at the NEW gids,
    a fresh name table, post v3.0 (no glyph names) and OS/2 with fsType cleared
    so the font is legally embeddable,
  * drop everything that only matters for screen shaping: GPOS/GSUB/GDEF/FFTM/
    vhea/vmtx/EBDT/EBLC/EBSC/SVG and any table we do not understand.

Validated on the 4.6 MB WenQuanYi Micro Hei that cajviewer ships (49531 glyphs,
long loca, numberOfHMetrics 48634 < numGlyphs).
"""
import struct

from . import ttfsub

__all__ = ["subset", "build_cmap"]

build_cmap = ttfsub.build_cmap

# Tables the subset carries, in emit order. Hinting tables are kept when the
# source has them: dropping `fpgm`/`prep` while keeping `cvt ` (or vice versa)
# can make a rasterizer run stale bytecode.
_KEEP = ("head", "hhea", "maxp", "OS/2", "hmtx", "cmap", "loca", "glyf",
         "name", "post", "cvt ", "fpgm", "prep", "gasp")

_MORE_COMPONENTS = 0x0020
_ARG_1_AND_2_ARE_WORDS = 0x0001
_WE_HAVE_A_SCALE = 0x0008
_WE_HAVE_AN_X_AND_Y_SCALE = 0x0040
_WE_HAVE_A_TWO_BY_TWO = 0x0080


def _u16(d, o):
    return struct.unpack_from(">H", d, o)[0]


def _i16(d, o):
    return struct.unpack_from(">h", d, o)[0]


# --------------------------------------------------------------------------
# source parsing
# --------------------------------------------------------------------------
def _read_loca(data, glyf_off, loca_off, num_glyphs, long_format):
    """Absolute glyf offsets, length num_glyphs + 1."""
    out = []
    if long_format:
        for i in range(num_glyphs + 1):
            out.append(glyf_off + struct.unpack_from(">I", data,
                                                     loca_off + 4 * i)[0])
    else:
        for i in range(num_glyphs + 1):
            out.append(glyf_off + 2 * struct.unpack_from(">H", data,
                                                         loca_off + 2 * i)[0])
    return out


def _read_hmtx(data, hmtx_off, num_glyphs, num_metrics):
    """[(advance, lsb)] for every gid. Gids past num_metrics reuse the last
    advance -- that is what numberOfHMetrics < numGlyphs means."""
    out = []
    at = hmtx_off
    last_adv = 0
    for i in range(num_glyphs):
        if i < num_metrics:
            last_adv = _u16(data, at)
            lsb = _i16(data, at + 2)
            at += 4
        else:
            lsb = _i16(data, at)
            at += 2
        out.append((last_adv, lsb))
    return out


def _component_gids(glyph, num_glyphs):
    """Old gids referenced by a composite glyph's component records."""
    if len(glyph) < 10:
        return []
    contours = struct.unpack_from(">h", glyph, 0)[0]
    if contours >= 0:
        return []                      # simple glyph
    out = []
    at = 10                            # past the 5x int16 header
    while True:
        if at + 4 > len(glyph):
            break
        flags = _u16(glyph, at)
        gid = _u16(glyph, at + 2)
        at += 4
        if flags & _ARG_1_AND_2_ARE_WORDS:
            at += 4
        else:
            at += 2
        if flags & _WE_HAVE_A_TWO_BY_TWO:
            at += 8
        elif flags & _WE_HAVE_AN_X_AND_Y_SCALE:
            at += 4
        elif flags & _WE_HAVE_A_SCALE:
            at += 2
        if gid < num_glyphs:
            out.append(gid)
        if not (flags & _MORE_COMPONENTS):
            break
    return out


def _rewrite_components(glyph, remap, num_glyphs):
    """A copy of a composite glyph with component indices remapped."""
    contours = struct.unpack_from(">h", glyph, 0)[0]
    if contours >= 0:
        return glyph
    out = bytearray(glyph)
    at = 10
    while True:
        if at + 4 > len(out):
            break
        flags = _u16(out, at)
        gid = _u16(out, at + 2)
        struct.pack_into(">H", out, at + 2, remap.get(gid, 0))
        at += 4
        if flags & _ARG_1_AND_2_ARE_WORDS:
            at += 4
        else:
            at += 2
        if flags & _WE_HAVE_A_TWO_BY_TWO:
            at += 8
        elif flags & _WE_HAVE_AN_X_AND_Y_SCALE:
            at += 4
        elif flags & _WE_HAVE_A_SCALE:
            at += 2
        if not (flags & _MORE_COMPONENTS):
            break
    del num_glyphs
    return bytes(out)


# --------------------------------------------------------------------------
# fresh tables
# --------------------------------------------------------------------------
def _build_cmap_table(code_to_gid):
    """A cmap with a format 4 BMP subtable and, when needed, format 12.

    Both are present whenever there is anything non-BMP, and the BMP one is
    always emitted because plenty of consumers only look at (3,1).
    """
    codes = sorted(code_to_gid)
    bmp = {c: g for c, g in code_to_gid.items() if c <= 0xFFFF}

    # ---- format 4 -------------------------------------------------------
    # One segment per contiguous run of codepoints with contiguous gids, plus
    # a 0xFFFF terminator segment. An empty map is legal: the terminator
    # segment alone maps everything to .notdef.
    segs = []
    run = []
    for c in sorted(bmp):
        if run and c == run[-1] + 1 and \
                bmp[c] == bmp[run[-1]] + 1:
            run.append(c)
        else:
            if run:
                segs.append(run)
            run = [c]
    if run:
        segs.append(run)
    ends = [s[-1] for s in segs] + [0xFFFF]
    starts = [s[0] for s in segs] + [0xFFFF]
    deltas = [(bmp[s[0]] - s[0]) & 0xFFFF for s in segs] + [1]
    seg_count = len(ends)
    range_len = [0] * seg_count           # contiguous runs -> no range offset
    f4_body = bytearray()
    sr, es, rs = _cmap_search_params(seg_count)
    f4_body += struct.pack(">HHHH", seg_count * 2, sr, es, rs)
    for e in ends:
        f4_body += struct.pack(">H", e)
    f4_body += struct.pack(">H", 0)       # reservedPad
    for s in starts:
        f4_body += struct.pack(">H", s)
    for d in deltas:
        f4_body += struct.pack(">H", d)
    for r in range_len:
        f4_body += struct.pack(">H", r)
    fmt4 = struct.pack(">HHH", 4, 6 + len(f4_body), 0) + bytes(f4_body)

    subs = [(3, 1, fmt4), (0, 3, fmt4)]

    # ---- format 12 ------------------------------------------------------
    if codes and codes[-1] > 0xFFFF:
        groups = bytearray()
        run = []
        for c in codes:
            if run and c == run[-1] + 1 and \
                    code_to_gid[c] == code_to_gid[run[-1]] + 1:
                run.append(c)
            else:
                if run:
                    groups.extend(_f12_group(run, code_to_gid))
                run = [c]
        if run:
            groups.extend(_f12_group(run, code_to_gid))
        ngroups = len(groups) // 12
        fmt12 = struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, ngroups)
        fmt12 += bytes(groups)
        subs += [(3, 10, fmt12), (0, 4, fmt12)]

    # ---- header ---------------------------------------------------------
    # The sfnt spec requires encoding records sorted by ascending
    # (platformID, encodingID); readers binary-search this array.
    subs.sort(key=lambda s: (s[0], s[1]))
    header = bytearray(struct.pack(">HH", 0, len(subs)))
    body = bytearray()
    off = 4 + 8 * len(subs)
    for pid, eid, blob in subs:
        header += struct.pack(">HHI", pid, eid, off)
        body += blob
        off += len(blob)
    return bytes(header) + bytes(body)


def _f12_group(run, code_to_gid):
    return struct.pack(">III", run[0], run[-1], code_to_gid[run[0]])


def _entry_selector(n):
    es = 0
    while 2 ** (es + 1) <= max(n, 1):
        es += 1
    return es


def _cmap_search_params(seg_count):
    """cmap format 4 header fields; records are 2 bytes, not 16."""
    es = _entry_selector(seg_count)
    sr = 2 * (2 ** es)
    return sr, es, 2 * seg_count - sr


def _u16be(s):
    return s.encode("utf-16-be")


def _build_name(source, family):
    """A fresh name table: the source's strings, with the family replaced.

    Records for platformID 3 (UTF-16BE) and 1 (Mac Roman); nameIDs 0-8 plus
    11/13/14 when the source had them.
    """
    src_names = {}
    off = ttfsub.table_offset(source, "name")
    if off is not None:
        count, str_off = struct.unpack_from(">HH", source, off + 2)
        for i in range(min(count, 512)):
            pid, _eid, _lid, nid, ln, o = struct.unpack_from(
                ">6H", source, off + 6 + 12 * i)
            if pid not in (1, 3) or nid in src_names or ln > 2000:
                continue
            blob = source[off + str_off + o: off + str_off + o + ln]
            try:
                s = blob.decode("utf-16-be") if pid == 3 else blob.decode(
                    "mac-roman")
            except (UnicodeDecodeError, LookupError):
                continue
            s = s.strip()
            if s:
                src_names[nid] = s

    ps = "".join(ch for ch in family if ch.isascii() and
                 (ch.isalnum() or ch in "-_")).strip("-_") or "Font"
    names = {
        0: src_names.get(0, "Subset generated by caj2pdf."),
        1: family,
        2: src_names.get(2, "Regular"),
        3: src_names.get(3, "caj2pdf;%s" % ps),
        4: family,
        5: src_names.get(5, "Version 1.0"),
        6: ps,
    }
    for nid in (7, 8, 11, 13, 14):
        if nid in src_names:
            names[nid] = src_names[nid]

    records = []
    storage = bytearray()

    def add(pid, eid, lid, nid, blob):
        o = len(storage)
        storage.extend(blob)
        records.append(struct.pack(">6H", pid, eid, lid, nid, len(blob), o))

    for nid, val in sorted(names.items()):
        add(3, 1, 0x0409, nid, _u16be(val))
        try:
            add(1, 0, 0, nid, val.encode("mac-roman"))
        except UnicodeEncodeError:
            add(1, 0, 0, nid, val.encode("ascii", "replace"))
    records.sort(key=lambda r: struct.unpack(">HHHH", r[:8]))
    head = struct.pack(">HHH", 0, len(records), 6 + 12 * len(records))
    return head + b"".join(records) + bytes(storage)


def _build_post(source_post):
    """post v3.0: the 32-byte fixed header, no glyph names."""
    if len(source_post) >= 12:
        italic = struct.unpack_from(">i", source_post, 4)[0]
        ul_pos = _i16(source_post, 8)
        ul_th = _i16(source_post, 10)
    else:
        italic, ul_pos, ul_th = 0, -100, 50
    fixed = struct.unpack_from(">I", source_post, 12)[0] \
        if len(source_post) >= 16 else 0
    return struct.pack(">IiiII", 0x00030000, italic, ul_pos, ul_th,
                       fixed) + b"\0" * 16


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def subset(ttf_bytes, codepoints, *, keep_notdef=True, family=None):
    """Build a subset of `ttf_bytes` covering `codepoints`.

    Returns a dict: bytes / code_to_gid / gid_to_adv / upem / metrics /
    num_glyphs / missing. See the module docstring for what is rewritten.
    """
    from . import _pdf

    data = bytes(ttf_bytes)
    tables = ttfsub.read_tables(data)
    for need in ("head", "hhea", "maxp", "cmap", "hmtx", "loca", "glyf"):
        if need not in tables:
            raise ValueError("source font has no %r table" % need)

    head_off = tables["head"][0]
    upem = _u16(data, head_off + 18)
    if not upem:
        raise ValueError("head.unitsPerEm is 0")
    long_loca = struct.unpack_from(">h", data, head_off + 50)[0] == 1

    num_glyphs = _u16(data, tables["maxp"][0] + 4)
    num_metrics = _u16(data, tables["hhea"][0] + 34)
    num_metrics = min(num_metrics or num_glyphs, num_glyphs)

    glyf_off, glyf_len = tables["glyf"][0], tables["glyf"][1]
    loca = _read_loca(data, glyf_off, tables["loca"][0], num_glyphs, long_loca)
    hmtx = _read_hmtx(data, tables["hmtx"][0], num_glyphs, num_metrics)

    src_cmap = build_cmap(data)
    codepoints = sorted({int(c) for c in codepoints if int(c) > 0})

    # ---- codepoint -> old gid -------------------------------------------
    code_to_old = {}
    missing = []
    for cp in codepoints:
        g = src_cmap.get(cp, 0)
        if g and g < num_glyphs:
            code_to_old[cp] = g
        else:
            missing.append(cp)

    # ---- glyph closure over composites ----------------------------------
    def raw(gid):
        a, b = loca[gid], loca[gid + 1]
        if b <= a:
            return b""
        return data[a:b]

    wanted = set(code_to_old.values())
    if keep_notdef or not wanted:
        wanted.add(0)
    keep = set()
    stack = sorted(wanted)
    while stack:
        g = stack.pop()
        if g in keep or g >= num_glyphs:
            continue
        keep.add(g)
        for comp in _component_gids(raw(g), num_glyphs):
            if comp not in keep:
                stack.append(comp)

    # ---- new gid numbering: .notdef first, then ascending ----------------
    ordered = ([0] if keep_notdef and 0 in keep else [])
    ordered += [g for g in sorted(keep) if g not in set(ordered)]
    old_to_new = {old: new for new, old in enumerate(ordered)}
    new_num = len(ordered)
    if new_num == 0:
        raise ValueError("subset would contain no glyphs")

    code_to_gid = {cp: old_to_new[g] for cp, g in code_to_old.items()}
    gid_to_adv = {old_to_new[g]: int(hmtx[g][0]) for g in ordered}

    # ---- glyf ------------------------------------------------------------
    # Every glyph starts on a 4-byte boundary: long loca requires it, and short
    # loca (offsets stored /2) needs at least even alignment.
    glyf_out = bytearray()
    new_offsets = [0]
    for old in ordered:
        blob = raw(old)
        if blob:
            blob = _rewrite_components(blob, old_to_new, num_glyphs)
            pad = (-len(glyf_out)) % 4
            if pad:
                glyf_out.extend(b"\0" * pad)
                new_offsets[-1] += pad
            glyf_out.extend(blob)
        new_offsets.append(len(glyf_out))

    # ---- loca ------------------------------------------------------------
    # Short loca stores offset/2, so offsets must be even; the %4 padding above
    # guarantees that. Use long loca whenever any offset exceeds uint16*2.
    use_long = max(new_offsets) > 0x1FFFE
    if use_long:
        loca_out = b"".join(struct.pack(">I", o) for o in new_offsets)
    else:
        loca_out = b"".join(struct.pack(">H", o // 2) for o in new_offsets)
    # Pad to 4 so the next table starts aligned.
    loca_out += b"\0" * ((-len(loca_out)) % 4)

    # ---- hmtx: full metrics for every glyph, then collapse the tail ------
    adv_list = [hmtx[old][0] for old in ordered]
    lsb_list = [hmtx[old][1] for old in ordered]
    new_num_metrics = new_num
    while new_num_metrics > 1 and adv_list[new_num_metrics - 1] == \
            adv_list[new_num_metrics - 2]:
        new_num_metrics -= 1
    hmtx_out = bytearray()
    for i in range(new_num):
        if i < new_num_metrics:
            hmtx_out += struct.pack(">Hh", adv_list[i], lsb_list[i])
        else:
            hmtx_out += struct.pack(">h", lsb_list[i])

    # ---- head / hhea / maxp / OS2 ---------------------------------------
    # Field offsets (all big-endian):
    #   head: 8 checkSumAdjustment, 16 flags, 18 unitsPerEm, 20 created,
    #         28 modified, 36 xMin/yMin/xMax/yMax (4x int16), 50 indexToLocFormat
    #   hhea: 4 ascender, 6 descender, 8 lineGap, 10 advanceWidthMax,
    #         12 minLeftSideBearing, 14 minRightSideBearing, 16 xMaxExtent,
    #         18 caretSlopeRise, 20 caretSlopeRun, 34 numberOfHMetrics
    #   maxp: 4 numGlyphs
    #   OS/2: 8 fsType, 66 usFirstCharIndex, 68 usLastCharIndex
    head = bytearray(data[head_off:head_off + tables["head"][1]])
    struct.pack_into(">H", head, 50, 1 if use_long else 0)
    # created/modified zeroed so the same input always yields the same bytes.
    struct.pack_into(">Q", head, 20, 0)
    struct.pack_into(">Q", head, 28, 0)
    # xMin/yMin/xMax/yMax over the kept glyphs only.
    xmn = ymn = xmx = ymx = None
    for old in ordered:
        blob = raw(old)
        if len(blob) >= 10:
            gx0, gy0, gx1, gy1 = struct.unpack_from(">4h", blob, 2)
            xmn = gx0 if xmn is None else min(xmn, gx0)
            ymn = gy0 if ymn is None else min(ymn, gy0)
            xmx = gx1 if xmx is None else max(xmx, gx1)
            ymx = gy1 if ymx is None else max(ymx, gy1)
    if xmn is not None:
        struct.pack_into(">4h", head, 36, xmn, ymn, xmx, ymx)

    # Glyph bbox extremes over the kept glyphs, for hhea's derived fields:
    #   advanceWidthMax      max advance
    #   minLeftSideBearing   min lsb (only for glyphs with non-zero advance)
    #   minRightSideBearing  min (advance - xMax)
    #   xMaxExtent           max (lsb + (xMax - xMin))
    max_adv = max(adv_list) if adv_list else 0
    min_lsb = max_extent = None
    min_rsb = 0
    for old in ordered:
        adv, lsb = hmtx[old]
        if min_lsb is None or lsb < min_lsb:
            min_lsb = lsb
        blob = raw(old)
        if len(blob) < 10 or adv == 0:
            continue
        gx0, _gy0, gx1, _gy1 = struct.unpack_from(">4h", blob, 2)
        extent = lsb + (gx1 - gx0)
        if max_extent is None or extent > max_extent:
            max_extent = extent
        rsb = adv - gx1
        if rsb < min_rsb:
            min_rsb = rsb
    min_lsb = 0 if min_lsb is None else min_lsb
    max_extent = 0 if max_extent is None else max(max_extent, xmx or 0)

    hhea = bytearray(data[tables["hhea"][0]:
                         tables["hhea"][0] + tables["hhea"][1]])
    struct.pack_into(">H", hhea, 10, max_adv)
    struct.pack_into(">h", hhea, 12, min_lsb)
    struct.pack_into(">h", hhea, 14, min_rsb)
    struct.pack_into(">h", hhea, 16, max_extent)
    struct.pack_into(">H", hhea, 34, new_num_metrics)

    # maxp: only numGlyphs changes. The rest are upper bounds (maxPoints,
    # maxStorage, maxFunctionDefs, ...) and staying generous is legal -- and
    # necessary, since we keep fpgm/prep and their function definitions.
    maxp = bytearray(data[tables["maxp"][0]:
                         tables["maxp"][0] + tables["maxp"][1]])
    struct.pack_into(">H", maxp, 4, new_num)

    os2 = None
    if "OS/2" in tables:
        o, ln = tables["OS/2"][0], tables["OS/2"][1]
        os2 = bytearray(data[o:o + ln])
        struct.pack_into(">H", os2, 8, 0)              # fsType: installable
        if len(os2) >= 70 and code_to_gid:
            first = min(min(code_to_gid), 0xFFFF)
            last = min(max(code_to_gid), 0xFFFF)
            struct.pack_into(">H", os2, 66, first)
            struct.pack_into(">H", os2, 68, last)

    post_blob = b""
    if "post" in tables:
        po, pl = tables["post"][0], tables["post"][1]
        post_blob = data[po:po + pl]

    out_tables = {
        "head": bytes(head),
        "hhea": bytes(hhea),
        "maxp": bytes(maxp),
        "hmtx": bytes(hmtx_out),
        "cmap": _build_cmap_table(code_to_gid),
        "loca": loca_out,
        "glyf": bytes(glyf_out),
        "name": _build_name(data, family or ttfsub.family_name(data) or "Font"),
        "post": _build_post(post_blob),
    }
    if os2 is not None:
        out_tables["OS/2"] = bytes(os2)
    for opt in ("cvt ", "fpgm", "prep", "gasp"):
        if opt in tables:
            o, ln = tables[opt][0], tables[opt][1]
            out_tables[opt] = data[o:o + ln]

    order = [t for t in _KEEP if t in out_tables]
    out = ttfsub.assemble(out_tables, order)

    metrics = _pdf.parse_ttf_metrics(out)
    return {
        "bytes": out,
        "code_to_gid": code_to_gid,
        "gid_to_adv": gid_to_adv,
        "upem": int(upem),
        "metrics": metrics,
        "num_glyphs": new_num,
        "missing": sorted(missing),
        "source_num_glyphs": num_glyphs,
        "composites_pulled_in": len(keep - wanted),
    }
