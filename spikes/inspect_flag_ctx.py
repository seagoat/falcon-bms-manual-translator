"""Lead: inspect the neighbourhood of the two flagged TO-1 segments."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

from core import db as cdb, store  # noqa: E402

con = cdb.connect()
segs = store.segments_of_version(con, 2)
tr = store.translations_of_version(con, 2)
by = {int(s["id"]): i for i, s in enumerate(segs)}

for sid in (14398, 14556):
    i = by[sid]
    print("=" * 74)
    for j in range(max(0, i - 1), min(len(segs), i + 3)):
        s = segs[j]
        t = tr.get(s["id"])
        mark = ">>" if int(s["id"]) == sid else "  "
        print(f"{mark} seg={s['id']} p{s['page']} {s['kind']:10s} y={s['bbox'][1]:7.1f}")
        print(f"      EN: {s['text']!r}")
        print(f"      ZH: {(t['text'] if t else None)!r}")
con.close()
