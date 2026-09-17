"""Lead: compare TOC page 4 — original vs exported CN — to see the page-number column."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402

ORIG = "origin/BMS-Training-Manual_v2_demo.pdf"
CN = "data/derived/full_cn.pdf"
PAGE = 4

o = pymupdf.open(ORIG)
c = pymupdf.open(CN)

print(f"=== 原文 p{PAGE} 全部文字块（含 x 范围）===")
for b in o[PAGE - 1].get_text("dict")["blocks"]:
    if b["type"] != 0:
        continue
    for ln in b.get("lines", []):
        t = "".join(s["text"] for s in ln["spans"])
        if t.strip():
            bb = ln["bbox"]
            print(f"  x {bb[0]:6.1f}-{bb[2]:6.1f}  {t.strip()[:70]!r}")

print(f"\n=== 中文 p{PAGE} 全部文字块 ===")
for b in c[PAGE - 1].get_text("dict")["blocks"]:
    if b["type"] != 0:
        continue
    for ln in b.get("lines", []):
        t = "".join(s["text"] for s in ln["spans"])
        if t.strip():
            bb = ln["bbox"]
            print(f"  x {bb[0]:6.1f}-{bb[2]:6.1f}  {t.strip()[:70]!r}")

# 并排图
dpi = 130
ao = o[PAGE - 1].get_pixmap(dpi=dpi)
ac = c[PAGE - 1].get_pixmap(dpi=dpi)
io = Image.frombytes("RGB", (ao.width, ao.height), ao.samples)
ic = Image.frombytes("RGB", (ac.width, ac.height), ac.samples)
gap = 18
canvas = Image.new("RGB", (ao.width + gap + ac.width, max(ao.height, ac.height)),
                   (40, 40, 40))
canvas.paste(io, (0, 0))
canvas.paste(ic, (ao.width + gap, 0))
out = f"data/derived/preview/_toc_cmp_p{PAGE}.png"
canvas.save(out)
print(f"\n对比图 -> {out}")
o.close()
c.close()
