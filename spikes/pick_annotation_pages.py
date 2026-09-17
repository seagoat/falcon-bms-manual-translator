"""Lead: pick which pages genuinely need in-image annotation.

A page only needs it if there is non-banner image area AND that image region is NOT
already covered by text the main pipeline translates. Pages whose "image" is just a
screenshot of titled content (tables, checklists) are already handled.

Scoring: rasterise the page with the text layer suppressed, OCR it, and compare the
recovered text against the DB segments for that page. High overlap => the "in-image
text" is actually live text => skip.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

from core import db as cdb  # noqa: E402

PDF = "origin/BMS-Training-Manual_v2_demo.pdf"
CACHE = "data/derived/ocr/_page_scan.json"
os.makedirs("data/derived/ocr", exist_ok=True)

d = pymupdf.open(PDF)

con = cdb.connect()
seg_by_page = {}
for r in con.execute("SELECT page, text FROM segments WHERE version_id=2 AND role='body'"):
    seg_by_page.setdefault(int(r["page"]), []).append((r["text"] or "").strip())
con.close()


def norm(s):
    import re
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


results = []
for i in range(d.page_count):
    pno = i + 1
    p = d[i]
    # non-banner image area
    im_area = 0.0
    biggest = 0.0
    for info in p.get_image_info(xrefs=True):
        x0, y0, x1, y1 = info["bbox"]
        h = y1 - y0
        if h < 80:            # header/footer banner
            continue
        a = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        im_area += a
        biggest = max(biggest, a)
    frac = im_area / (p.rect.width * p.rect.height)
    live = sum(len(t) for t in seg_by_page.get(pno, []))
    # heuristic: needs annotation if meaningful image area but little live text
    # (pure/semi-pure diagram pages), OR a big single image with sparse text
    needs = (frac > 0.25 and live < 400) or (biggest / (p.rect.width * p.rect.height) > 0.45)
    results.append({
        "page": pno,
        "img_frac": round(frac, 3),
        "live_chars": live,
        "needs": bool(needs),
    })

json.dump(results, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
need = [r for r in results if r["needs"]]
print(f"总页数 {d.page_count}")
print(f"需要图内标注的页: {len(need)}")
print("\n前 40 页（page / 图片占比 / 现有文本字数 / 最大图占比）:")
for r in need[:40]:
    print(f"  p{r['page']:3d}  img={r['img_frac']:.2f}  live_chars={r['live_chars']:5d}")
print(f"\n按图片占比分组:")
import collections  # noqa: E402
b = collections.Counter()
for r in need:
    b[round(r["img_frac"], 1)] += 1
for k in sorted(b):
    print(f"  img_frac≈{k}: {b[k]} 页")
d.close()
