"""Lead: measure OCR + annotation cost across a sample of image-heavy pages.

Decides the full-book rollout strategy: how many pages actually have in-image text,
per-page OCR time, and estimated total cost.
"""
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

PDF = "origin/BMS-Training-Manual_v2_demo.pdf"
OUT = "data/derived/ocr/_probe"
os.makedirs(OUT, exist_ok=True)

d = pymupdf.open(PDF)

# candidate pages: non-banner image area > 25% OR very little extractable text
cand = []
for i in range(d.page_count):
    p = d[i]
    pa = p.rect.width * p.rect.height
    im_area = 0.0
    for info in p.get_image_info(xrefs=True):
        x0, y0, x1, y1 = info["bbox"]
        if (y1 - y0) < 80:
            continue
        if i == 0:
            im_area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
            continue
        im_area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    frac = im_area / pa
    chars = len(p.get_text().strip())
    if frac > 0.25 or chars < 200:
        cand.append((i + 1, round(frac, 2), chars))

print(f"candidate pages (image-heavy or text-poor): {len(cand)}")
print(f"  of which chars<200 (pure image pages): {sum(1 for c in cand if c[2] < 200)}")

random.seed(7)
sample = random.sample(cand, min(8, len(cand)))
print(f"\nsampling {len(sample)} for OCR timing: {[s[0] for s in sample]}")

from rapidocr_onnxruntime import RapidOCR  # noqa: E402
t0 = time.perf_counter()
ocr = RapidOCR()
print(f"engine init: {time.perf_counter()-t0:.1f}s")

tot_boxes = 0
tot_t = 0.0
per_page = []
for pno, frac, chars in sorted(sample):
    p = d[pno - 1]
    big = None
    for info in p.get_image_info(xrefs=True):
        x0, y0, x1, y1 = info["bbox"]
        if (y1 - y0) > 80:
            big = info
            break
    if big is None:
        clip = pymupdf.Rect(0, 66, p.rect.width, 776)
    else:
        clip = pymupdf.Rect(big["bbox"])
    pix = p.get_pixmap(dpi=250, clip=clip)
    img = os.path.join(OUT, f"probe_{pno}.png")
    pix.save(img)
    t0 = time.perf_counter()
    res, _ = ocr(img)
    dt = time.perf_counter() - t0
    n = len(res) if res else 0
    tot_boxes += n
    tot_t += dt
    per_page.append((pno, frac, chars, n, dt))
    print(f"  p{pno:3d} img_frac={frac:.2f} text_chars={chars:5d} "
          f"-> {n:3d} boxes in {dt:5.2f}s")

print(f"\n=== totals over {len(per_page)} sampled pages ===")
print(f"  OCR boxes: {tot_boxes}  ({tot_boxes/max(1,len(per_page)):.1f}/page)")
print(f"  OCR time : {tot_t:.1f}s ({tot_t/max(1,len(per_page)):.2f}s/page)")
print(f"\n=== extrapolation for the full candidate set ({len(cand)} pages) ===")
print(f"  OCR time  : {tot_t/max(1,len(per_page))*len(cand)/60:.1f} min")
print(f"  OCR boxes : {tot_boxes/max(1,len(per_page))*len(cand):.0f} labels")
print(f"  translation: ~{tot_boxes/max(1,len(per_page))*len(cand)/12:.0f} batches "
      f"(12 labels/batch) -> est. ${tot_boxes/max(1,len(per_page))*len(cand)/12*0.0004:.2f}")
d.close()
