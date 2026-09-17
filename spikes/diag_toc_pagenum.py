"""Lead: investigate the TOC page-number rendering the user reports as broken.

The user's screenshot shows the TOC in BOTH panes with dot leaders but the
page-number column looking wrong. Check:
  1. does the source text still contain the page numbers?
  2. does the translation?
  3. what does the dot-leader fitter produce for these lines?
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402
from render import pagebuild as pb  # noqa: E402

con = cdb.connect()
segs = store.segments_of_version(con, 2, page=4)   # 训练手册 v2 目录页
tr = store.translations_of_version(con, 2)
print(f"p4 段数: {len(segs)}")
n_dots = 0
for s in segs[:14]:
    t = tr.get(s["id"])
    zh = (t or {}).get("text") or ""
    en = s["text"] or ""
    has_dots = "...." in en
    if has_dots:
        n_dots += 1
    print(f"\nseg={s['id']} {s['kind']:10s} {'DOTS' if has_dots else '    '} "
          f"lines={len(s.get('line_boxes') or [])}")
    print(f"  EN: {en[:88]!r}")
    print(f"  ZH: {zh[:88]!r}")
    # 点线拟合结果
    if has_dots and zh:
        try:
            fitted, w = pb._fit_dot_leader(zh, 9.2, False, 470.0, s["bbox"][2], s["bbox"][0])
            print(f"  fit: {fitted[:88]!r}  w={w:.1f}")
        except Exception as e:
            print(f"  fit ERR: {e}")
con.close()
