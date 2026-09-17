"""Lead: final verification of the two deliverable PDFs."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

BI = "data/derived/full_bi.pdf"
CN = "data/derived/full_cn.pdf"
ORIG = "origin/BMS-Training-Manual_v2_demo.pdf"

bi = pymupdf.open(BI)
cn = pymupdf.open(CN)
o = pymupdf.open(ORIG)

print("=== artifacts ===")
for p, d in ((BI, bi), (CN, cn), (ORIG, o)):
    print(f"  {os.path.basename(p):32s} pages={d.page_count:4d} "
          f"size={os.path.getsize(p)/1e6:6.2f} MB")

print("\n=== page geometry ===")
print(f"  original   {o[0].rect}")
print(f"  cn         {cn[0].rect}")
print(f"  bilingual  {bi[0].rect}   width ratio = {bi[0].rect.width / o[0].rect.width:.3f}")
print(f"  bilingual p200 width ratio = {bi[199].rect.width / o[199].rect.width:.3f}")

print("\n=== content checks (page 21) ===")
for label, d in (("cn", cn), ("bilingual", bi)):
    t = d[20].get_text()
    cjk = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
    lat = len([w for w in t.split() if w.isascii() and len(w) > 3])
    print(f"  {label:10s}: cjk_chars={cjk:5d}  ascii_words={lat:4d}  total_chars={len(t)}")

print("\n=== TOC / metadata ===")
print(f"  original  TOC entries: {len(o.get_toc())}")
print(f"  cn        TOC entries: {len(cn.get_toc())}")
print(f"  bilingual TOC entries: {len(bi.get_toc())}")
print(f"  cn metadata title: {cn.metadata.get('title')!r}")

print("\n=== bilingual halves both populated (sample pages) ===")
for i in (0, 20, 100, 300, 400):
    page = bi[i]
    pix = page.get_pixmap(dpi=60)
    import numpy as np
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    mid = arr.shape[1] // 2
    left, right = arr[:, :mid], arr[:, mid:]
    nz_l = int((left.min(axis=2) < 240).sum())
    nz_r = int((right.min(axis=2) < 240).sum())
    print(f"  p{i+1:3d}: left_nonwhite={nz_l:6d}  right_nonwhite={nz_r:6d}  "
          f"{'OK' if nz_l > 500 and nz_r > 500 else 'SUSPECT'}")

bi.close(); cn.close(); o.close()
