"""Lead: render a page of the produced CN PDF next to the original for visual QA,
and detect real collisions (a translated line bleeding past the next segment's top)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

CN = sys.argv[1]
ORIG = sys.argv[2]
PAGES = [int(x) for x in sys.argv[3].split(",")]
OUT = sys.argv[4] if len(sys.argv) > 4 else "data/derived/preview"
DPI = 110

os.makedirs(OUT, exist_ok=True)
cn = pymupdf.open(CN)
orig = pymupdf.open(ORIG)
print(f"CN={CN} pages={cn.page_count}  ORIG pages={orig.page_count}")

problems = 0
for pno in PAGES:
    cp = cn[pno - 1]
    op = orig[pno - 1]

    # collect text lines in reading order
    def lines_of(page):
        out = []
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for ln in b.get("lines", []):
                spans = [s for s in ln["spans"] if s["text"].strip()]
                if spans:
                    out.append((ln["bbox"], "".join(s["text"] for s in spans)))
        out.sort(key=lambda x: (round(x[0][1], 1), x[0][0]))
        return out

    cn_lines = lines_of(cp)
    print(f"\n===== page {pno}: CN has {len(cn_lines)} text lines")

    # build "next segment top" reference from ORIGINAL paragraph layout:
    # original English text lines tell us where each paragraph starts.
    o_lines = lines_of(op)
    tops = sorted({round(bb[1], 1) for bb, _ in o_lines})
    collisions = []
    for i in range(len(cn_lines) - 1):
        bb, txt = cn_lines[i]
        nbb, ntxt = cn_lines[i + 1]
        # A real collision needs BOTH vertical overlap AND horizontal overlap.
        # Table rows produce several cells on one baseline at disjoint x ranges —
        # those are not collisions.
        x_overlap = min(bb[2], nbb[2]) - max(bb[0], nbb[0])
        if x_overlap <= 0:
            continue
        if bb[3] > nbb[1] + 1.5:
            collisions.append((bb, txt, nbb, ntxt))
    if collisions:
        problems += len(collisions)
        print(f"  !! {len(collisions)} overlapping line pairs")
        for bb, txt, nbb, ntxt in collisions[:6]:
            print(f"     {txt[:58]!r}")
            print(f"        y{bb[1]:.1f}-{bb[3]:.1f} vs next y{nbb[1]:.1f} ({ntxt[:40]!r})")
    else:
        print("  OK no line overlaps")

    # side-by-side image: original left | gap | translated right.
    # NOTE: pymupdf.Pixmap.copy() silently drops content when the target rect is
    # offset (it maps source->target as a scaled blit, not a paste). Use Pillow.
    from PIL import Image

    a = op.get_pixmap(dpi=DPI)
    b = cp.get_pixmap(dpi=DPI)
    gap = 24
    ia = Image.frombytes("RGB", (a.width, a.height), a.samples)
    ib = Image.frombytes("RGB", (b.width, b.height), b.samples)
    out = Image.new("RGB", (a.width + gap + b.width, max(a.height, b.height)), (68, 68, 68))
    out.paste(ia, (0, 0))
    out.paste(ib, (a.width + gap, 0))
    path = os.path.join(OUT, f"cmp_p{pno}.png")
    out.save(path)
    print(f"  saved {path}  ({out.width}x{out.height})")

print(f"\nTOTAL overlapping line pairs: {problems}")
cn.close()
orig.close()
sys.exit(1 if problems else 0)
