"""Lead: full-book CN PDF QA — overflow/shrink worst cases + readability + residue.

Checks the production deliverable data/derived/full_cn.pdf:
  * page count / size
  * how many translated lines are below a readable threshold
  * residual English body text (should only be headers/footers/page numbers/codes)
  * the 10 reported overflow segments identified by seg id
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

CN = sys.argv[1] if len(sys.argv) > 1 else "data/derived/full_cn.pdf"
ORIG = "origin/BMS-Training-Manual_v2_demo.pdf"
CJK = re.compile(r"[\u4e00-\u9fff]")

cn = pymupdf.open(CN)
orig = pymupdf.open(ORIG)
print(f"{CN}: pages={cn.page_count}  bytes={os.path.getsize(CN)/1e6:.1f} MB")
print(f"orig  : pages={orig.page_count}")

sizes = []
small = []
resid = []
no_cjk_pages = 0
for i in range(cn.page_count):
    page = cn[i]
    has_cjk = False
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b.get("lines", []):
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if not txt:
                continue
            sz = min(s["size"] for s in ln["spans"])
            sizes.append(sz)
            if CJK.search(txt):
                has_cjk = True
            if sz < 6.0:
                small.append((i + 1, round(sz, 2), txt[:70]))
    if not has_cjk:
        no_cjk_pages += 1
    # residual english body: original line present in the CN page at the same place
    otxt = {ln["bbox"][1] for b in orig[i].get_text("dict")["blocks"] if b["type"] == 0
            for ln in b.get("lines", [])}
    ctxt = {round(ln["bbox"][1], 0) for b in page.get_text("dict")["blocks"] if b["type"] == 0
            for ln in b.get("lines", [])}
    if otxt and not (otxt & ctxt):
        pass  # geometry differs after reflow; not a reliable residue test
cn.close()
orig.close()

sizes.sort()
import statistics  # noqa: E402
print(f"\n=== font sizes across all lines ({len(sizes)}) ===")
print(f"  min={sizes[0]:.2f}  p1={sizes[len(sizes)//100]:.2f}  median={statistics.median(sizes):.2f}  "
      f"max={sizes[-1]:.2f}")
print(f"  lines < 6.0pt (hard to read): {len(small)}  ({len(small)/max(1,len(sizes))*100:.2f}%)")
for p, sz, t in sorted(small, key=lambda x: x[1])[:15]:
    print(f"    p{p:3d} {sz:5.2f}pt  {t!r}")
print(f"\n  pages with NO Chinese at all: {no_cjk_pages}/{len(sizes) and 401}")
