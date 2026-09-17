"""Lead: spike - OCR the cockpit diagram on p11 with rapidocr (already installed).

Goal: find out whether in-image labels can be located reliably enough to annotate.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

PDF = "origin/BMS-Training-Manual_v2_demo.pdf"
OUT = "data/derived/ocr"
os.makedirs(OUT, exist_ok=True)

PAGE = int(sys.argv[1]) if len(sys.argv) > 1 else 11
DPI = int(sys.argv[2]) if len(sys.argv) > 2 else 300

d = pymupdf.open(PDF)
p = d[PAGE - 1]

# the big diagram image (skip header/footer banners). Some pages are made of several
# smaller images or no image at all -> fall back to OCR'ing the whole body region.
big = None
for info in p.get_image_info(xrefs=True):
    x0, y0, x1, y1 = info["bbox"]
    if (y1 - y0) > 80:
        big = info
        break
if big is None:
    print(f"page {PAGE}: no single large image -> OCR the body region instead")
    raster = p.get_pixmap(dpi=DPI, clip=pymupdf.Rect(0, 66, p.rect.width, 776))
    img = os.path.join(OUT, f"p{PAGE}_body_{DPI}.png")
    raster.save(img)
    big = {"xref": -1, "bbox": [0, 66, p.rect.width, 776],
           "width": raster.width, "height": raster.height}
else:
    print(f"page {PAGE}: diagram xref={big['xref']} bbox={[round(v) for v in big['bbox']]} "
          f"px={big['width']}x{big['height']}")
    raster = p.get_pixmap(dpi=DPI, clip=pymupdf.Rect(big["bbox"]))
    img = os.path.join(OUT, f"p{PAGE}_diagram_{DPI}.png")
    raster.save(img)
print(f"saved {img} ({raster.width}x{raster.height})")
d.close()

print("\n=== loading rapidocr (first run downloads/initialises models) ===")
t0 = time.perf_counter()
from rapidocr_onnxruntime import RapidOCR  # noqa: E402
ocr = RapidOCR()
print(f"  init {time.perf_counter()-t0:.1f}s")

t0 = time.perf_counter()
result, elapse = ocr(img)
print(f"  OCR {time.perf_counter()-t0:.1f}s  elapsed_detail={elapse}")
if not result:
    print("  !! no text detected")
    sys.exit(0)

print(f"\n=== {len(result)} text boxes detected ===")
rows = []
for box, text, score in result:
    xs = [pt[0] for pt in box]
    ys = [pt[1] for pt in box]
    rows.append({
        "text": text, "score": float(score),
        "x0": round(min(xs), 1), "y0": round(min(ys), 1),
        "x1": round(max(xs), 1), "y1": round(max(ys), 1),
        "h": round(max(ys) - min(ys), 1),
    })
rows.sort(key=lambda r: (r["y0"], r["x0"]))
for r in rows:
    print(f"  [{r['score']:.2f}] ({r['x0']:6.1f},{r['y0']:6.1f})-({r['x1']:6.1f},{r['y1']:6.1f}) "
          f"h={r['h']:5.1f}  {r['text']!r}")

import json  # noqa: E402
path = os.path.join(OUT, f"p{PAGE}_ocr.json")
json.dump({"page": PAGE, "dpi": DPI, "image_bbox": list(big["bbox"]),
           "texts": rows}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\nsaved {path}")
