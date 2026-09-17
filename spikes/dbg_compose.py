"""Debug: why is the right panel of the side-by-side blank?"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

cn = pymupdf.open("data/derived/real_cn_1_31.pdf")
orig = pymupdf.open("origin/BMS-Training-Manual.pdf")
cp = cn[20]
op = orig[20]

b = cp.get_pixmap(dpi=110)
print("cn pixmap:", b.width, b.height, "alpha:", b.alpha, "n:", b.n)
b.save("data/derived/preview/_dbg_cn_only.png")

# sample some pixels from the middle of the cn page to confirm content exists
import collections  # noqa: E402

cnt = collections.Counter()
for y in range(0, b.height, 7):
    for x in range(0, b.width, 7):
        cnt[b.pixel(x, y)] += 1
print("cn top colors:", cnt.most_common(4))

a = op.get_pixmap(dpi=110)
cnt2 = collections.Counter()
for y in range(0, a.height, 7):
    for x in range(0, a.width, 7):
        cnt2[a.pixel(x, y)] += 1
print("orig top colors:", cnt2.most_common(4))

# try the composite again and sample the right half
gap = 24
w = a.width + gap + b.width
h = max(a.height, b.height)
canvas = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
canvas.clear_with(0x444444)
canvas.copy(a, pymupdf.IRect(0, 0, a.width, a.height))
canvas.copy(b, pymupdf.IRect(a.width + gap, 0, a.width + gap + b.width, b.height))
canvas.save("data/derived/preview/_dbg_canvas.png")
cnt3 = collections.Counter()
for y in range(0, h, 7):
    for x in range(a.width + gap, w, 7):
        cnt3[canvas.pixel(x, y)] += 1
print("right-half colors:", cnt3.most_common(4))

# alternate approach: use Pillow to compose
from PIL import Image  # noqa: E402

ia = Image.frombytes("RGB", (a.width, a.height), a.samples)
ib = Image.frombytes("RGB", (b.width, b.height), b.samples)
out = Image.new("RGB", (a.width + gap + b.width, h), (68, 68, 68))
out.paste(ia, (0, 0))
out.paste(ib, (a.width + gap, 0))
out.save("data/derived/preview/_dbg_pil.png")
print("PIL composite saved; size", out.size)
cn.close()
orig.close()
