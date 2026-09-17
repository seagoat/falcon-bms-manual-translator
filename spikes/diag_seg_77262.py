"""Lead: diagnose seg=77262 — a 2-line CN TOC box that overlaps its first line."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402
from render import pagebuild as pb  # noqa: E402

con = cdb.connect()
s = store.get_segment(con, 77262)
tr = store.translations_of_version(con, 2)
zh = (tr.get(77262) or {}).get("text") or ""
con.close()

print(f"seg=77262 page={s['page']} kind={s['kind']}")
print(f"  EN  : {s['text']!r}")
print(f"  ZH  : {zh!r}")
print(f"  bbox: {[round(v,1) for v in s['bbox']]}")
print(f"  line_boxes: {[[round(v,1) for v in lb] for lb in s['line_boxes']]}")
print(f"  style: {s.get('style')}")
print()
print(f"  _has_dot_leader(EN) = {pb._has_dot_leader(s['text'])}")
print(f"  _has_dot_leader(ZH) = {pb._has_dot_leader(zh)}")
print(f"  _split_dot_leader(ZH) = {pb._split_dot_leader(zh)}")
print()
print("=== 这段的相邻段（看接下来的 8.2 是否被并进来）===")
con = cdb.connect()
for sid in range(77259, 77267):
    x = store.get_segment(con, sid)
    if x:
        z = (tr.get(sid) or {}).get("text") or ""
        print(f"  seg={sid} p{x['page']} kind={x['kind']:10s} y={x['bbox'][1]:6.1f}")
        print(f"      EN={x['text'][:64]!r}")
        print(f"      ZH={z[:64]!r}")
con.close()
