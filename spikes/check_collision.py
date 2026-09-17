"""Lead: detect layout collisions in the produced CN PDF.

For every page: read the rendered translated page's text lines and check whether any
line's vertical span overlaps the next segment's original bbox (i.e. text bleeds into
the following paragraph/heading).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

SUBSET = sys.argv[1] if len(sys.argv) > 1 else r"data/derived/_smoke_cn.pdf"
SRC = r"origin/BMS-Training-Manual.pdf"
PAGES = [int(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["21", "22", "23"])]

doc = pymupdf.open(SUBSET)
print(f"subset {SUBSET}: {doc.page_count} pages")
src = pymupdf.open(SRC)

for pno in PAGES:
    p = doc[pno - 1]
    lines = []
    for b in p.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b.get("lines", []):
            txt = "".join(s["text"] for s in ln["spans"])
            if txt.strip():
                lines.append((ln["bbox"], txt))
    lines.sort(key=lambda x: (x[0][1], x[0][0]))
    print(f"\n===== CN page {pno}: {len(lines)} text lines")
    prev = None
    for bb, txt in lines:
        warn = ""
        if prev is not None and bb[1] < prev[0] - 0.5:
            warn = f"  <<< OVERLAP with previous line (prev y1={prev[0]:.1f})"
        print(f"  y {bb[1]:6.1f}-{bb[3]:6.1f} x {bb[0]:6.1f}-{bb[2]:6.1f} {txt[:70]!r}{warn}")
        prev = (bb[3], bb[1])
doc.close()
src.close()
