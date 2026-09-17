"""Lead: quantify text embedded in raster images (the user's question 2).

For each page: total image area vs page area, and how much extractable text exists.
Images whose rasterised size is small relative to their placement are likely diagrams
with baked-in labels (not extractable, not translatable by the current pipeline).
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np  # noqa: E402
import pymupdf  # noqa: E402

PDF = "origin/BMS-Training-Manual_v2_demo.pdf"
d = pymupdf.open(PDF)

big_images = 0
pages_with_big_images = []
label_like = 0
total_imgs = 0
rows = []
for i in range(d.page_count):
    p = d[i]
    pa = p.rect.width * p.rect.height
    infos = p.get_image_info(xrefs=True)
    if not infos:
        continue
    total_imgs += len(infos)
    im_area = 0.0
    for info in infos:
        x0, y0, x1, y1 = info["bbox"]
        w = max(0.0, x1 - x0)
        h = max(0.0, y1 - y0)
        a = w * h
        # skip the repeating header/footer banners (full width, tiny height)
        if h < 80:
            continue
        im_area += a
    frac = im_area / pa
    chars = len(p.get_text().strip())
    if frac > 0.30:
        big_images += 1
        pages_with_big_images.append((i + 1, round(frac, 2), chars))
    rows.append((i + 1, round(frac, 3), chars, len(infos)))

print(f"pages={d.page_count}  image placements={total_imgs}")
print(f"pages where non-banner image area > 30% of the page: {big_images}")
print("\nsample of image-heavy pages (page, image-area fraction, extractable chars):")
for r in pages_with_big_images[:30]:
    print(f"  p{r[0]:3d}  {r[1]:.2f}  chars={r[2]}")

# Text-looking pages: high extractable text AND large image => likely diagram labels
print("\n=== pages with BOTH lots of extractable text and large images ===")
both = [r for r in rows if r[2] > 400 and r[1] > 0.25]
print(f"count={len(both)}")
for r in both[:25]:
    print(f"  p{r[0]:3d} img_frac={r[1]:.2f} chars={r[2]} imgs={r[3]}")

# The specific cockpit page the user showed
print("\n=== page 11 (cockpit diagram, user's screenshot) ===")
p = d[10]
print(f"  extractable text chars: {len(p.get_text().strip())}")
print(f"  text: {' '.join(p.get_text().split())[:160]!r}")
for info in p.get_image_info(xrefs=True):
    x0, y0, x1, y1 = info["bbox"]
    print(f"  image xref={info['xref']} bbox=({x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}) "
          f"px={info['width']}x{info['height']}  "
          f"px_per_pt={info['width']/max(1,(x1-x0)):.2f}")
print("  -> 图中 LEFT INDEXER / MASTER CAUTION LIGHT 等标签是像素，不是文本层")

d.close()
