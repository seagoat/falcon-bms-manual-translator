"""Lead: render the样例 annotated pages (p11 座舱图, p114-117 纯图页, p291 MFD 页).

Produces both a PNG (for quick viewing) and a PDF (for zoom/text-check), plus a
side-by-side original|annotated comparison image.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402

from render import annotate  # noqa: E402

SRC = "origin/BMS-Training-Manual_v2_demo.pdf"
OUT = "data/derived/preview"
PDF_OUT = "data/derived/annotated_sample.pdf"
PAGES = [int(x) for x in sys.argv[1:]] or [11, 114, 115, 116, 117, 291]
DPI = int(os.environ.get("ANNOT_DPI", "130"))

os.makedirs(OUT, exist_ok=True)
src = pymupdf.open(SRC)
out = pymupdf.open()
report = []

for pno in PAGES:
    if pno < 1 or pno > src.page_count:
        print(f"p{pno}: out of range")
        continue
    stat = annotate.annotate_page(src, pno, out, verbose=True)
    report.append(stat)
    print(f"p{pno}: planned={stat['planned']} drawn={stat['drawn']}")

out.save(PDF_OUT)
print(f"\nsaved {PDF_OUT} ({len(report)} pages)")

# render comparisons
for idx, stat in enumerate(report):
    pno = stat["page"]
    a = src[pno - 1].get_pixmap(dpi=DPI)
    b = out[idx].get_pixmap(dpi=DPI)
    ia = Image.frombytes("RGB", (a.width, a.height), a.samples)
    ib = Image.frombytes("RGB", (b.width, b.height), b.samples)
    gap = 20
    canvas = Image.new("RGB", (a.width + gap + b.width, max(a.height, b.height)),
                       (40, 40, 40))
    canvas.paste(ia, (0, 0))
    canvas.paste(ib, (a.width + gap, 0))
    p = os.path.join(OUT, f"annot_cmp_p{pno}.png")
    canvas.save(p)
    out[idx].get_pixmap(dpi=DPI).save(os.path.join(OUT, f"annot_p{pno}.png"))
    print(f"  saved {p}")

json.dump([{k: v for k, v in s.items() if k != "items"} | {"items": s["items"]}
           for s in report],
          open("data/derived/ocr/annotate_report.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print("saved data/derived/ocr/annotate_report.json")
src.close()
out.close()
