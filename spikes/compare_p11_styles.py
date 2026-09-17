"""Lead: render p11 in both annotation styles for a side-by-side comparison."""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf
from PIL import Image

from render import annotate as A

SRC = "origin/BMS-Training-Manual_v2_demo.pdf"
src = pymupdf.open(SRC)
imgs = []
for style in ("capsule", "numbered"):
    out = pymupdf.open()
    st = A.annotate_page(src, 11, out, style=style)
    pix = out[0].get_pixmap(dpi=125)
    path = f"data/derived/preview/p11_{style}.png"
    pix.save(path)
    print(f"{style:9s}: planned={st['planned']} drawn={st['drawn']} used={st['style']} -> {path}")
    imgs.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    out.close()
src.close()

# 三栏对比：原图 | 胶囊风格 | 编号风格
orig = pymupdf.open(SRC)
op = orig[10].get_pixmap(dpi=125)
oi = Image.frombytes("RGB", (op.width, op.height), op.samples)
orig.close()

gap = 14
w = oi.width + gap + imgs[0].width + gap + imgs[1].width
h = max(oi.height, imgs[0].height, imgs[1].height)
canvas = Image.new("RGB", (w, h), (35, 35, 35))
canvas.paste(oi, (0, 0))
canvas.paste(imgs[0], (oi.width + gap, 0))
canvas.paste(imgs[1], (oi.width + gap + imgs[0].width + gap, 0))
canvas.save("data/derived/preview/p11_styles_compare.png")
print(f"\n三栏对比图: data/derived/preview/p11_styles_compare.png ({w}x{h})")
for f in ("p11_capsule.png", "p11_numbered.png"):
    import os
    p = f"data/derived/preview/{f}"
    print(f"  {f}: {os.path.getsize(p)/1024:.0f} KB")
