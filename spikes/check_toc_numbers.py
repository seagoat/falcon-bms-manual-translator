"""Lead: check whether TOC page numbers survive in the CN PDF, and whether the
dot leaders now collide with them."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

from core import db as cdb, store  # noqa: E402

CN = "data/derived/full_cn.pdf"
PAGE = 4
c = pymupdf.open(CN)
print(f"=== 中文 p{PAGE} 右侧区（x>500）的文字 ===")
found = 0
for b in c[PAGE - 1].get_text("dict")["blocks"]:
    if b["type"] != 0:
        continue
    for ln in b.get("lines", []):
        t = "".join(s["text"] for s in ln["spans"]).strip()
        bb = ln["bbox"]
        if t and bb[2] > 500:
            found += 1
            print(f"  x {bb[0]:6.1f}-{bb[2]:6.1f} y {bb[1]:6.1f}  {t[:60]!r}")
print(f"  共 {found} 行")

print(f"\n=== 数据库里 p{PAGE} 的「页码」段 ===")
con = cdb.connect()
segs = store.segments_of_version(con, 2, page=PAGE)
tr = store.translations_of_version(con, 2)
for s in segs:
    txt = (s["text"] or "").strip()
    if txt.isdigit() or (len(txt) <= 4 and txt.replace(".", "").isdigit()):
        t = tr.get(s["id"])
        zh = (t or {}).get("text")
        print(f"  seg={s['id']} kind={s['kind']:10s} role={s['role']:6s} "
              f"bbox={[round(v,1) for v in s['bbox']]} EN={txt!r} ZH={zh!r} "
              f"status={(t or {}).get('status')}")
# 所有 p4 段的 x1 分布（看点线右端与页码左端是否打架）
print(f"\n=== p{PAGE} 所有段的 bbox.x1 分布（看是否有点线越过页码）===")
for s in sorted(segs, key=lambda x: x["bbox"][1]):
    txt = (s["text"] or "").strip()
    if "...." in txt:
        print(f"  DOTS  x1={s['bbox'][2]:6.1f} y0={s['bbox'][1]:6.1f} {txt[:38]!r}")
con.close()
c.close()
