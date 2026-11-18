#!/usr/bin/env python3
"""End-to-end test for caj2pdf against the vendored CNKI runtime.

Converts tests/fixtures/中国金融体制改革阶段研究_李卉.caj in all three modes and
checks each produced PDF from scratch (re/struct/zlib only -- no PDF library):

  text    4 pages; ToUnicode extraction recovers the exact expected per-page
          character counts; the document title is present; no unmapped glyphs
  image   4 pages; the raster decodes (incl. PNG predictor) and is NOT blank --
          ink pixels far above the ~3k floor of the inkless-render regression
          that motivated the mandatory CAJFILE_SetBaseFont call
  hybrid  4 pages; raster present AND an invisible (3 Tr) text layer extracts

Objects are located through the xref table (exact byte offsets), never by
scanning for 'endobj'/'N 0 obj', because compressed binary streams routinely
contain those literal byte sequences.

Usage:
    tests/e2e_test.py [--lib PATH] [--resdir DIR] [--workdir DIR]

Defaults: lib/resdir from CAJREADER_LIB / CAJVIEWER_DIR env vars (which the
Docker image sets), else cajlib's own search; workdir = a fresh tempdir.
Exit code 0 iff every check passed.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import zlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(REPO, "tests", "fixtures",
                       "中国金融体制改革阶段研究_李卉.caj")
EXPECTED_PAGES = 4
EXPECTED_CHARS = (1302, 1555, 1470, 1642)   # per page, from CAJFILE_GetPageInfo
TITLE = "中国金融体制改革阶段研究"
MIN_INK_PX = 30000        # inked px across all pages; blank render is ~3k

failures = []


def check(name, ok, detail=""):
    print("%s  %-44s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        failures.append(name)
    return bool(ok)


# --------------------------------------------------------------- PDF parsing
def pdf_objects(data):
    """{obj number: body bytes} using exact xref offsets."""
    m = re.search(rb"startxref\s+(\d+)", data)
    if not m:
        raise ValueError("no startxref")
    seg = data[int(m.group(1)):]
    if not seg.startswith(b"xref"):
        raise ValueError("startxref does not point at an xref table")
    entries = {}
    i = 4
    while i < len(seg):
        hm = re.match(rb"\s*(\d+)\s+(\d+)\s+", seg[i:])
        if not hm:
            break
        first, count = int(hm.group(1)), int(hm.group(2))
        i += hm.end()
        for k in range(count):
            row = seg[i:i + 20]
            i += 20
            if len(row) >= 18 and row[17:18] == b"n":
                entries[first + k] = int(row[0:10])
    nums = sorted(entries)
    out = {}
    for idx, num in enumerate(nums):
        off = entries[num]
        nxt = entries[nums[idx + 1]] if idx + 1 < len(nums) else len(data)
        hm = re.match(rb"%d\s+0\s+obj\b" % num, data[off:off + 40])
        out[num] = data[off + (hm.end() if hm else 0):nxt]
    return out


def _png_unfilter(data, colors, columns):
    """Undo PNG row filters (types 0-4) for an 8-bpc image."""
    rowlen, bpp = colors * columns, colors
    out = bytearray()
    prev = bytearray(rowlen)
    i, n = 0, len(data)
    while i < n:
        ft = data[i]
        i += 1
        cur = bytearray(data[i:i + rowlen])
        i += rowlen
        if len(cur) < rowlen:
            break
        for x in range(rowlen):
            a = cur[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ft == 1:
                cur[x] = (cur[x] + a) & 255
            elif ft == 2:
                cur[x] = (cur[x] + b) & 255
            elif ft == 3:
                cur[x] = (cur[x] + ((a + b) >> 1)) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[x] = (cur[x] + pr) & 255
        out += cur
        prev = cur
    return bytes(out)


def pdf_stream(body):
    """Decoded payload of a stream object body, honouring /Length + predictor."""
    si = body.find(b"stream")
    if si < 0:
        return None
    dic = body[:si]
    j = body.find(b"\n", si) + 1
    ln = re.search(rb"/Length\s+(\d+)", dic)
    if ln:
        raw = body[j:j + int(ln.group(1))]
    else:
        raw = body[j:body.rfind(b"endstream")].rstrip(b"\r\n")
    if b"/FlateDecode" in dic:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            return raw
    pm = re.search(rb"/DecodeParms\s*<<(.*?)>>", dic, re.S)
    if pm:
        pred = re.search(rb"/Predictor\s+(\d+)", pm.group(1))
        if pred and int(pred.group(1)) >= 10:
            cols = re.search(rb"/Columns\s+(\d+)", pm.group(1))
            colors = re.search(rb"/Colors\s+(\d+)", pm.group(1))
            bpc = re.search(rb"/BitsPerComponent\s+(\d+)", pm.group(1))
            if cols and (not bpc or int(bpc.group(1)) == 8):
                raw = _png_unfilter(raw, int(colors.group(1)) if colors else 1,
                                    int(cols.group(1)))
    return raw


def tounicode_map(body):
    s = pdf_stream(body)
    m = {}
    if not s:
        return m
    for blk in re.finditer(rb"beginbfchar(.*?)endbfchar", s, re.S):
        for a, b in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>",
                               blk.group(1)):
            m[int(a, 16)] = bytes.fromhex(b.decode()).decode("utf-16-be",
                                                             "replace")
    for blk in re.finditer(rb"beginbfrange(.*?)endbfrange", s, re.S):
        for a, b, c in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>",
                blk.group(1)):
            lo, hi, base = int(a, 16), int(b, 16), int(c, 16)
            for i, cp in enumerate(range(lo, hi + 1)):
                m[cp] = chr(base + i)
    return m


def page_count(data):
    return len(re.findall(rb"/Type\s*/Page\b(?!s)", data))


def extract_text(data):
    """Per-page text via each page's font ToUnicode map."""
    objs = pdf_objects(data)
    tunis = {}
    for num, body in objs.items():
        if b"/Type0" in body:
            mu = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
            tunis[num] = tounicode_map(objs[int(mu.group(1))]) if mu else {}
    pages = []
    for num, body in sorted(objs.items()):
        if not (re.search(rb"/Type\s*/Page\b(?!s)", body)
                and b"/MediaBox" in body):
            continue
        mc = re.search(rb"/Contents\s+(\d+)\s+0\s+R", body)
        if not mc:
            pages.append("")
            continue
        keymap = {int(v) for _k, v in
                  re.findall(rb"/(F\d+)\s+(\d+)\s+0\s+R", body)}
        cmap = {}
        for fnum in keymap:
            cmap.update(tunis.get(fnum, {}))
        stream = pdf_stream(objs[int(mc.group(1))]) or b""
        out = []
        for t in re.finditer(rb"\[([^\]]*)\]\s*TJ|<([0-9A-Fa-f]+)>\s*Tj",
                             stream, re.S):
            payload = t.group(1) if t.group(1) is not None else t.group(2)
            pieces = (re.findall(rb"<([0-9A-Fa-f]+)>", payload)
                      if t.group(1) is not None else [payload])
            for pc in pieces:
                bs = bytes.fromhex(pc.decode())
                for i in range(0, len(bs) - 1, 2):
                    out.append(cmap.get((bs[i] << 8) | bs[i + 1], "?"))
        pages.append("".join(out))
    return pages


def count_ink(data):
    """Dark pixels summed over every image XObject."""
    total = 0
    for _num, body in pdf_objects(data).items():
        if not re.search(rb"/Subtype\s*/Image\b", body):
            continue
        w = int(re.search(rb"/Width\s+(\d+)", body).group(1))
        h = int(re.search(rb"/Height\s+(\d+)", body).group(1))
        mb = re.search(rb"/BitsPerComponent\s+(\d+)", body)
        bpc = int(mb.group(1)) if mb else 8
        raw = pdf_stream(body)
        if raw is None:
            continue
        if bpc == 8 and len(raw) == w * h:          # DeviceGray, 8-bit
            total += sum(1 for b in raw if b < 100)
        elif len(raw) == w * h:                     # other 8-bit single plane
            total += sum(1 for b in raw if b < 100)
    return total


def has_render_mode3(data):
    for _num, body in pdf_objects(data).items():
        s = pdf_stream(body)
        if s and re.search(rb"\b3\s+Tr\b", s) and b"TJ" in s:
            return True
    return False


# ---------------------------------------------------------------------- main
def run_convert(src, dst, mode, lib, resdir):
    cmd = [sys.executable, os.path.join(REPO, "caj2pdf.py"), src,
           "-o", dst, "--mode", mode, "--dpi", "150"]
    if lib:
        cmd += ["--lib", lib]
    if resdir:
        cmd += ["--resdir", resdir]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        sys.stdout.write(p.stdout[-3000:])
        sys.stderr.write(p.stderr[-3000:])
    return p.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default=os.environ.get("CAJREADER_LIB"))
    ap.add_argument("--resdir", default=os.environ.get("CAJVIEWER_DIR"))
    ap.add_argument("--workdir")
    args = ap.parse_args()

    if not check("fixture present", os.path.isfile(FIXTURE), FIXTURE):
        return 1
    work = args.workdir or tempfile.mkdtemp(prefix="caj2pdf-e2e-")
    outs = {}

    for mode in ("text", "image", "hybrid"):
        dst = os.path.join(work, "e2e_%s.pdf" % mode)
        ok = run_convert(FIXTURE, dst, mode, args.lib, args.resdir)
        outs[mode] = dst
        if not check("convert --mode %s exits 0" % mode, ok,
                     "%d bytes" % (os.path.getsize(dst) if ok else -1)):
            continue
        data = open(dst, "rb").read()
        check("%s: PDF envelope (%%PDF-1.7 + %%%%EOF)" % mode,
              data.startswith(b"%PDF-1.7") and b"%%EOF" in data[-32:])
        check("%s: %d page objects" % (mode, EXPECTED_PAGES),
              page_count(data) == EXPECTED_PAGES)

    if os.path.isfile(outs["text"]):
        pages = extract_text(open(outs["text"], "rb").read())
        check("text: per-page char counts",
              tuple(len(p) for p in pages) == EXPECTED_CHARS,
              "got %s want %s" % (tuple(len(p) for p in pages),
                                  EXPECTED_CHARS))
        check("text: title recovered", bool(pages) and TITLE in pages[0],
              (pages[0][:40] + "...") if pages else "")
        check("text: no unmapped glyphs", all("?" not in p for p in pages))

    if os.path.isfile(outs["image"]):
        ink = count_ink(open(outs["image"], "rb").read())
        check("image: raster not blank", ink > MIN_INK_PX, "%d ink px" % ink)

    if os.path.isfile(outs["hybrid"]):
        hdata = open(outs["hybrid"], "rb").read()
        check("hybrid: raster not blank", count_ink(hdata) > MIN_INK_PX)
        check("hybrid: invisible text layer (3 Tr + TJ)",
              has_render_mode3(hdata))
        hpages = extract_text(hdata)
        check("hybrid: text layer extracts",
              sum(len(p) for p in hpages) == sum(EXPECTED_CHARS),
              "%d chars" % sum(len(p) for p in hpages))

    print("\n%s" % ("ALL PASSED" if not failures
                    else "FAILURES: " + ", ".join(failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
