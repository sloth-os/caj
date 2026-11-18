"""TrueType helpers: sfnt directory, cmap and name-table parsing, subsetting.

Pure stdlib -- this environment has no fontTools. The parsing here is written
against the sfnt spec and validated on the 4.6 MB WenQuanYi Micro Hei that the
CAJ viewer ships (49531 glyphs, long loca, numberOfHMetrics < numGlyphs).
"""
import struct

__all__ = [
    "read_tables", "table_offset", "build_cmap", "font_names", "postscript_name",
    "family_name", "checksum", "assemble", "subset", "TTFCmap",
]

TAG_ORDER = ("head", "hhea", "maxp", "OS/2", "hmtx", "cmap", "loca", "glyf",
             "name", "post", "cvt ", "fpgm", "prep", "gasp")


# --------------------------------------------------------------------------
# sfnt directory
# --------------------------------------------------------------------------
def read_tables(data):
    """tag -> (offset, length, checksum) for every table in the directory."""
    if data[:4] == b"ttcf":
        # A TrueType Collection: offset 12 starts an offset table, not a table
        # directory. Extract the wanted face with an external tool first.
        raise ValueError("ttcf collection; pass a single sfnt face, not a TTC")
    if data[:4] not in (b"\x00\x01\x00\x00", b"true"):
        raise ValueError("not a TrueType sfnt (magic %s)" % data[:4].hex())
    num = struct.unpack_from(">H", data, 4)[0]
    out = {}
    for i in range(num):
        tag, ck, off, ln = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        out[tag.decode("latin1")] = (off, ln, ck)
    return out


def table_offset(data, tag):
    """Offset of `tag` in `data`, or None."""
    t = read_tables(data).get(tag)
    return t[0] if t else None


def checksum(blob):
    """The sfnt table checksum: sum of big-endian uint32 over zero-padded data."""
    blob = blob + b"\0" * ((-len(blob)) % 4)
    return sum(struct.unpack(">%dI" % (len(blob) // 4), blob)) & 0xFFFFFFFF


def assemble(tables, order=None):
    """Pack {tag: bytes} into an sfnt with correct checksums and alignment.

    The head table's directory checksum is computed with checkSumAdjustment set
    to zero (per the sfnt spec), and the adjustment written last is whatever
    makes the WHOLE file sum to 0xB1B0AFBA.
    """
    order = [t for t in (order or TAG_ORDER) if t in tables]
    order += [t for t in sorted(tables) if t not in order]
    n = len(order)
    entry_sel = 0
    while 2 ** (entry_sel + 1) <= max(n, 1):
        entry_sel += 1
    search_range = (2 ** entry_sel) * 16
    directory = bytearray()
    body = bytearray()
    pos = 12 + 16 * n
    head_off = None
    for tag in order:
        blob = bytearray(tables[tag])
        if tag == "head" and len(blob) >= 12:
            struct.pack_into(">I", blob, 8, 0)     # excluded from its checksum
        blob = bytes(blob)
        pad = (-pos) % 4
        if pad:
            body.extend(b"\0" * pad)
            pos += pad
        directory.extend(struct.pack(">4sIII", tag.encode("latin1"),
                                     checksum(blob), pos, len(blob)))
        body.extend(blob)
        if tag == "head":
            head_off = pos
        pos += len(blob)
    out = bytearray(struct.pack(">IHHHH", 0x00010000, n, search_range,
                                entry_sel, n * 16 - search_range))
    out += directory
    out += body
    if head_off is None:
        return bytes(out)
    adj = (0xB1B0AFBA - checksum(bytes(out))) & 0xFFFFFFFF
    struct.pack_into(">I", out, head_off + 8, adj)
    return bytes(out)


# --------------------------------------------------------------------------
# cmap
# --------------------------------------------------------------------------
class TTFCmap:
    """A parsed character map: codepoint -> glyph id."""

    def __init__(self, mapping, subtables):
        self._m = mapping
        self.subtables = subtables       # [(pid, eid, format, offset), ...]

    def __contains__(self, cp):
        return cp in self._m

    def get(self, cp, default=None):
        return self._m.get(cp, default)

    def __getitem__(self, cp):
        return self._m[cp]

    def __iter__(self):
        return iter(self._m)

    def __len__(self):
        return len(self._m)

    def items(self):
        return self._m.items()

    def keys(self):
        return self._m.keys()

    def values(self):
        return self._m.values()


def _parse_fmt4(data, base):
    """Windows BMP segments. Returns {codepoint: gid}."""
    seg_x2 = struct.unpack_from(">H", data, base + 6)[0]
    seg_count = seg_x2 // 2
    ends_at = base + 14
    starts_at = ends_at + seg_x2 + 2          # + reservedPad
    deltas_at = starts_at + seg_x2
    ranges_at = deltas_at + seg_x2
    out = {}
    for i in range(seg_count):
        end = struct.unpack_from(">H", data, ends_at + 2 * i)[0]
        start = struct.unpack_from(">H", data, starts_at + 2 * i)[0]
        delta = struct.unpack_from(">h", data, deltas_at + 2 * i)[0]
        roff = struct.unpack_from(">H", data, ranges_at + 2 * i)[0]
        if start == 0xFFFF:
            continue
        for cp in range(start, end + 1):
            if roff == 0:
                gid = (cp + delta) & 0xFFFF
            else:
                # idRangeOffset points into the range array itself: the glyph id
                # lives at rangeStart + offset + 2*(cp - start).
                at = ranges_at + 2 * i + roff + 2 * (cp - start)
                if at + 2 > len(data):
                    continue
                g = struct.unpack_from(">H", data, at)[0]
                if g == 0:
                    continue
                gid = (g + delta) & 0xFFFF
            if gid:
                out[cp] = gid
    return out


def _parse_fmt12(data, base):
    """Sequential map groups: full 32-bit codepoints."""
    ngroups = struct.unpack_from(">I", data, base + 12)[0]
    out = {}
    at = base + 16
    for _ in range(ngroups):
        if at + 12 > len(data):
            break
        lo, hi, start_gid = struct.unpack_from(">III", data, at)
        at += 12
        for cp in range(lo, hi + 1):
            out[cp] = start_gid + (cp - lo)
    return out


def _parse_fmt6(data, base):
    first, count = struct.unpack_from(">HH", data, base + 6)
    out = {}
    at = base + 10
    for i in range(count):
        if at + 2 > len(data):
            break
        g = struct.unpack_from(">H", data, at)[0]
        at += 2
        if g:
            out[first + i] = g
    return out


def _parse_fmt0(data, base):
    out = {}
    for i in range(256):
        g = data[base + 6 + i]
        if g:
            out[i] = g
    return out


def _parse_fmt2(data, base):
    """Apple's mixed 8/16-bit CJK map. Rare; parse conservatively."""
    out = {}
    sub_head_at = base + 6 + 512
    max_sub = 0
    for i in range(256):
        off = struct.unpack_from(">H", data, base + 6 + 2 * i)[0]
        if off and off // 8 > max_sub:
            max_sub = off // 8
    for hi in range(256):
        off = struct.unpack_from(">H", data, base + 6 + 2 * hi)[0]
        if not off:
            continue
        sh = base + 6 + off
        first, count, idelta = struct.unpack_from(">HHh", data, sh + 2)
        for lo in range(count):
            cp = (hi << 8) | (first + lo)
            g = struct.unpack_from(">H", data, sh + 8 + 2 * lo)[0]
            if g:
                out[cp] = (g + idelta) & 0xFFFF
    del sub_head_at, max_sub
    return out


_PARSERS = {0: _parse_fmt0, 2: _parse_fmt2, 4: _parse_fmt4,
            6: _parse_fmt6, 12: _parse_fmt12}

# Preference: a Unicode full-repertoire table first (3,10 / 0,4 / 0,6), then
# Windows BMP (3,1 / 0,3), then Mac Roman (1,0), then anything else.
_SUBTABLE_PREF = {(3, 10): 0, (0, 4): 0, (0, 6): 0, (3, 1): 1, (0, 3): 1,
                  (1, 0): 2}


def parse_cmap_subtables(data, cmap_off):
    """[(pid, eid, format, absolute_offset)] sorted best-first."""
    n = struct.unpack_from(">H", data, cmap_off + 2)[0]
    subs = []
    for i in range(n):
        pid, eid, rel = struct.unpack_from(">HHI", data, cmap_off + 4 + 8 * i)
        off = cmap_off + rel
        if off + 4 > len(data):
            continue
        fmt = struct.unpack_from(">H", data, off)[0]
        subs.append((pid, eid, fmt, off))
    subs.sort(key=lambda s: _SUBTABLE_PREF.get((s[0], s[1]), 3))
    return subs


def build_cmap(data, want_formats=(12, 4, 6, 0, 2)):
    """Return a TTFCmap built from the best available subtable.

    Later (worse-ranked) subtables fill in codepoints the best one misses, so a
    font whose only full-repertoire table is sparse still resolves everything.
    """
    off = table_offset(data, "cmap")
    if off is None:
        return TTFCmap({}, [])
    subs = parse_cmap_subtables(data, off)
    merged = {}
    used = []
    for fmt_pref in want_formats:
        for pid, eid, fmt, so in subs:
            if fmt != fmt_pref:
                continue
            parser = _PARSERS.get(fmt)
            if parser is None:
                continue
            try:
                m = parser(data, so)
            except struct.error:
                continue
            used.append((pid, eid, fmt, so))
            for cp, g in m.items():
                merged.setdefault(cp, g)
    return TTFCmap(merged, used)


# --------------------------------------------------------------------------
# name table
# --------------------------------------------------------------------------
def _decode_name(blob, pid):
    try:
        return blob.decode("utf-16-be") if pid == 3 else blob.decode("mac-roman")
    except (UnicodeDecodeError, LookupError):
        try:
            return blob.decode("utf-8")
        except UnicodeDecodeError:
            return blob.decode("latin1", "replace")


def font_names(data):
    """Every string in the name table (family, full, PostScript, ...)."""
    off = table_offset(data, "name")
    if off is None:
        return []
    count, str_off = struct.unpack_from(">HH", data, off + 2)
    out = []
    for i in range(min(count, 512)):
        pid, _eid, _lid, _nid, ln, o = struct.unpack_from(
            ">6H", data, off + 6 + 12 * i)
        blob = data[off + str_off + o: off + str_off + o + ln]
        if not blob:
            continue
        s = _decode_name(blob, pid).strip().strip("\x00")
        if s:
            out.append(s)
    return out


def _name_by_id(data, nids, prefer_platform=(3, 1)):
    off = table_offset(data, "name")
    if off is None:
        return None
    count, str_off = struct.unpack_from(">HH", data, off + 2)
    found = {}
    for i in range(min(count, 512)):
        pid, _eid, _lid, nid, ln, o = struct.unpack_from(
            ">6H", data, off + 6 + 12 * i)
        if nid not in nids:
            continue
        blob = data[off + str_off + o: off + str_off + o + ln]
        s = _decode_name(blob, pid).strip().strip("\x00")
        if s and nid not in found:
            found[nid] = s
        elif s and pid in prefer_platform and found.get(nid):
            pass
    for nid in nids:
        if found.get(nid):
            return found[nid]
    return None


def family_name(data):
    return _name_by_id(data, (1, 16, 21))


def postscript_name(data):
    return _name_by_id(data, (6,))


# --------------------------------------------------------------------------
# subsetting
# --------------------------------------------------------------------------
def subset(ttf_bytes, codepoints, *, keep_notdef=True):
    """Build a subset sfnt covering `codepoints`.

    Returns a dict:
        bytes        the subset font (starts with 0x00010000)
        code_to_gid  {codepoint -> gid IN THE SUBSET}
        gid_to_adv   {subset gid -> advance in font units}
        upem         int
        metrics      dict from cajpdf._pdf.parse_ttf_metrics() of the SUBSET
        num_glyphs   int
        missing      sorted codepoints with no glyph in the source
    """
    from . import _ttfsub_impl
    return _ttfsub_impl.subset(ttf_bytes, codepoints, keep_notdef=keep_notdef)
