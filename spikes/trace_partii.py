"""Lead: why does 'PART II - ...' become '第二部分 - 飞机武器投放系统与控制 2.'?"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

import pymupdf  # noqa: E402

from core import db as cdb, fingerprint as fp, store  # noqa: E402

con = cdb.connect()
segs = store.segments_of_version(con, 1)
tr = store.translations_of_version(con, 1)

TARGET = "PART II - AIRCRAFT WEAPON RELEASE SYSTEMS AND CONTROLS"
print(f"书签文本: {TARGET!r}")
print(f"指纹: {fp.fingerprint(TARGET)}")
hits = [s for s in segs if s["fingerprint"] == fp.fingerprint(TARGET)]
print(f"指纹命中 {len(hits)} 段:")
for s in hits:
    print(f"  seg={s['id']} p{s['page']} {s['kind']} EN={s['text']!r}")
    print(f"        ZH={(tr.get(s['id']) or {}).get('text')!r}")

# 该页附近的段顺序
for s in hits[:1]:
    ordered = sorted([x for x in segs if int(x["page"]) == int(s["page"])],
                     key=lambda x: int(x["order_index"]))
    idx = next((i for i, x in enumerate(ordered) if int(x["id"]) == int(s["id"])), None)
    print(f"\n该段在 p{s['page']} 的位置 #{idx}，前后各 3 段：")
    for j in range(max(0, idx - 1), min(len(ordered), idx + 4)):
        x = ordered[j]
        mark = ">>" if int(x["id"]) == int(s["id"]) else "  "
        print(f"{mark} #{j} seg={x['id']} {x['kind']:10s} "
              f"EN={x['text'][:52]!r}")
        print(f"        ZH={(tr.get(x['id']) or {}).get('text','')[:52]!r}")
con.close()
