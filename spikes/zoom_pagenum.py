"""Lead: visually compare TOC page 4 — original vs re-exported CN, zoomed on the
page-number column, to confirm the numbers are back and aligned."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402

ORIG = "origin/BMS-Training-Manual_v2_demo.pdf"
CN = "data/derived/full_cn.pdf"
PAGE = 4
DPI = 150

o = pymupdf.open(ORIG)
c = pymupdf.open(CN)
ao = o[PAGE - 1].get_pixmap(dpi=DPI)
ac = c[PAGE - 1].get_pixmap(dpi=DPI)
io = Image.frombytes("RGB", (ao.width, ao.height), ao.samples)
ic = Image.frombytes("RGB", (ac.width, ac.height), ac.samples)

gap = 18
canvas = Image.new("RGB", (ao.width + gap + ac.width, max(ao.height, ac.height)),
                   (40, 40, 40))
canvas.paste(io, (0, 0))
canvas.paste(ic, (ao.width + gap, 0))
full = "data/derived/preview/_toc_cmp_p4.png"
canvas.save(full)
print("整页对比 ->", full)

# 右半（页码列）放大裁切
scale = DPI / 72.0
x0 = int(480 * scale)
x1 = int(555 * scale)
left = io.crop((x0, 0, x1, min(io.height, int(700 * scale))))
right = ic.crop((x0, 0, x1, min(ic.height, int(700 * scale))))
w = left.width + 12 + right.width
h = max(left.height, right.height)
z = Image.new("RGB", (w, h), (40, 40, 40))
z.paste(left, (0, 0))
z.paste(right, (left.width + 12, 0))
zoom = "data/derived/preview/_toc_pagenum_zoom_p4.png"
z.save(zoom)
print("页码列放大对比 ->", zoom, f"({w}x{h})")

# 用文本层核对：中文 PDF 的页码列是否都是数字且 x 一致
print("\n=== 中文 PDF p4 页脚/页码列 ===")
xs = []
for b in c[PAGE - 1].get_text("dict")["blocks"]:
    if b["type"] != 0:
        continue
    for ln in b.get("lines", []):
        t = "".join(s["text"] for s in ln["spans"]).strip()
        if t and ln["bbox"][2] > 520:
            xs.append((round(ln["bbox"][0], 1), round(ln["bbox"][2], 1), t[-6:]))
print(f"  x>520 的行: {len(xs)}")
for x in xs[:12]:
    print(f"    x {x[0]:6.1f}-{x[1]:6.1f}  ...{x[2]!r}")
o.close()
c.close()
