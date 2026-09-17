"""Lead: show _fit_dot_leader output tails to see if the page number is preserved."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402
from render import pagebuild as pb  # noqa: E402

con = cdb.connect()
segs = store.segments_of_version(con, 2, page=4)
tr = store.translations_of_version(con, 2)
shown = 0
for s in segs:
    t = (s["text"] or "").strip()
    if "...." not in t:
        continue
    zh = (tr.get(s["id"]) or {}).get("text") or ""
    if not zh:
        continue
    lb = s["line_boxes"][-1]
    fitted, w = pb._fit_dot_leader(zh, 9.2, False, 470.0, lb[2], s["bbox"][0])
    print(f"seg={s['id']} anchor={lb[2]:.1f} w={w:.1f}")
    print(f"   ZH 尾: {zh[-24:]!r}")
    print(f"   fit尾: {fitted[-24:]!r}")
    shown += 1
    if shown >= 6:
        break
con.close()
