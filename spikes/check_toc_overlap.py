"""Lead: check the TOC page for OVERLAPPING text in our exported CN PDF.

Symptom to investigate: some dot lines' text appears to run into the next line's text
('... 153 8.2 上仰检'), which would mean two segments are drawn on top of each other.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf

from core import db as cdb, store  # noqa: E402
from render import pagebuild as pb  # noqa: E402

CN = "data/derived/full_cn.pdf"
PAGE = 4

d = pymupdf.open(CN)
print(f"=== CN p{PAGE}: 底部 12 行的 y / x 范围 ===")
rows = []
for b in d[PAGE - 1].get_text("dict")["blocks"]:
    if b["type"] != 0:
        continue
    for ln in b.get("lines", []):
        t = "".join(s["text"] for s in ln["spans"]).strip()
        if t:
            rows.append((ln["bbox"][1], ln["bbox"][0], ln["bbox"][2], t))
rows.sort()
for y, x0, x1, t in rows[-12:]:
    print(f"   y={y:6.1f} x={x0:6.1f}-{x1:6.1f}  {t[:70]!r}")

print(f"\n=== compute_layout 对同一页底部盒子的位置 ===")
con = cdb.connect()
segs = store.segments_of_version(con, 2, page=PAGE)
trs = store.translations_of_version(con, 2)
con.close()
boxes = pb.compute_layout(segs, trs, page_no=PAGE)
tail = sorted(boxes, key=lambda b: b["bbox"][1])[-10:]
info = {int(s["id"]): s for s in segs}
for b in tail:
    s = info.get(int(b["seg_id"]))
    print(f"   seg={b['seg_id']} y={b['bbox'][1]:6.1f} x={b['bbox'][0]:6.1f}-{b['bbox'][2]:6.1f} "
          f"lines={len(b['lines'])}")
    print(f"       EN={((s or {}).get('text') or '')[:60]!r}")
    for ln in b["lines"]:
        print(f"       y={ln['y']:6.1f} x={ln['x']:6.1f} w={ln['width']:6.1f} "
              f"{ln['text'][-34:]!r}")
d.close()
