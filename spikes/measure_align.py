"""Lead: measure vertical alignment of the CN page vs the original for a TOC page.

Compares each segment's original baseline (source PDF) with the drawn baseline in
the exported CN PDF, so misalignment is quantified rather than eyeballed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402
from core import db as cdb  # noqa: E402

PAGE = int(sys.argv[1]) if len(sys.argv) > 1 else 4
CN = "data/derived/full_cn.pdf"
ORIG = "origin/BMS-Training-Manual_v2_demo.pdf"

cn = pymupdf.open(CN)
orig = pymupdf.open(ORIG)

print(f"=== ORIGINAL page {PAGE} text lines (y0, text) ===")
o_lines = []
for b in orig[PAGE - 1].get_text("dict")["blocks"]:
    if b["type"] != 0:
        continue
    for ln in b.get("lines", []):
        t = "".join(s["text"] for s in ln["spans"]).strip()
        if t:
            o_lines.append((round(ln["bbox"][1], 1), round(ln["bbox"][3], 1), t))
o_lines.sort()
for y0, y1, t in o_lines[:26]:
    print(f"  y {y0:6.1f}-{y1:6.1f}  {t[:72]!r}")

print(f"\n=== CN page {PAGE} text lines ===")
c_lines = []
for b in cn[PAGE - 1].get_text("dict")["blocks"]:
    if b["type"] != 0:
        continue
    for ln in b.get("lines", []):
        t = "".join(s["text"] for s in ln["spans"]).strip()
        if t:
            c_lines.append((round(ln["bbox"][1], 1), round(ln["bbox"][3], 1), t))
c_lines.sort()
for y0, y1, t in c_lines[:26]:
    print(f"  y {y0:6.1f}-{y1:6.1f}  {t[:72]!r}")

print(f"\n=== DB segments on page {PAGE} (original bbox) ===")
con = cdb.connect()
rows = con.execute("SELECT id, order_index, kind, role, text, bbox, line_boxes FROM segments "
                   "WHERE version_id=2 AND page=? ORDER BY order_index", (PAGE,)).fetchall()
for r in rows[:22]:
    import json
    bb = json.loads(r["bbox"] or "[]")
    lb = json.loads(r["line_boxes"] or "[]")
    tr = con.execute("SELECT text FROM translations WHERE segment_id=?", (r["id"],)).fetchone()
    zh = (tr["text"] if tr else "") or ""
    print(f"  seg={r['id']} {r['kind']:10s} {r['role']:6s} "
          f"bbox=[{bb[0]:.1f},{bb[1]:.1f},{bb[2]:.1f},{bb[3]:.1f}] lines={len(lb)}")
    print(f"     EN: {r['text'][:66]!r}")
    print(f"     ZH: {zh[:66]!r}")
con.close()
cn.close()
orig.close()
