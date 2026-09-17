"""Lead: verify the bilingual PDF really contains EN | 中文 pages side by side."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402

BI = "data/derived/real_bi_1_31.pdf"
CN = "data/derived/real_cn_1_31_anchor.pdf"
ORIG = "origin/BMS-Training-Manual.pdf"

bi = pymupdf.open(BI)
cn = pymupdf.open(CN)
orig = pymupdf.open(ORIG)
print(f"bilingual: pages={bi.page_count}  size={os.path.getsize(BI)/1e6:.2f} MB")
print(f"cn       : pages={cn.page_count}  size={os.path.getsize(CN)/1e6:.2f} MB")
print(f"orig     : pages={orig.page_count}  size={os.path.getsize(ORIG)/1e6:.2f} MB")

for i in range(3):
    p = bi[i]
    o = orig[i]
    print(f"\nbilingual page {i+1}: rect={p.rect}  orig rect={o.rect}  "
          f"width ratio={p.rect.width/o.rect.width:.3f}")
    print(f"  images on page: {len(p.get_images(full=True))}")
    txt = p.get_text()
    cjk = sum(1 for ch in txt if "\u4e00" <= ch <= "\u9fff")
    latin_words = len([w for w in txt.split() if w.isascii() and len(w) > 3])
    print(f"  text chars={len(txt)} cjk={cjk} ascii_words={latin_words}")
    print(f"  sample: {txt[:150]!r}")

# render page 21 of the bilingual and inspect left/right halves
p = bi[20]
pix = p.get_pixmap(dpi=72)
img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
w, h = img.size
print(f"\nbilingual p21 bitmap {w}x{h}")
mid = w // 2
left = img.crop((0, 0, mid, h))
right = img.crop((mid, 0, w, h))
for name, part in (("left", left), ("right", right)):
    colors = part.getcolors(maxcolors=1 << 20) or []
    white = sum(c for c, col in colors if col >= (250, 250, 250))
    dark = sum(c for c, col in colors if sum(col) < 300)
    print(f"  {name}: white={white/ (part.width*part.height):.3f} dark={dark/(part.width*part.height):.4f}")

img.save("data/derived/preview/_bi_full_p21.png")
left.save("data/derived/preview/_bi_left_p21.png")
right.save("data/derived/preview/_bi_right_p21.png")
print("saved _bi_full_p21.png / _bi_left_p21.png / _bi_right_p21.png")
bi.close(); cn.close(); orig.close()
