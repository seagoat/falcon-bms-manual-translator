"""Lead: check whether the TO-1 'truncated' findings are real truncation or
legitimate continuations (bullet items split into two segments by extraction)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

from core import db as cdb, store  # noqa: E402

SUSPECT = [14103, 14398, 14556, 14841, 14944, 16038, 16294, 16338, 16394, 16427]

con = cdb.connect()
segs = store.segments_of_version(con, 2)
tr = store.translations_of_version(con, 2)
by_id = {int(s["id"]): i for i, s in enumerate(segs)}

for sid in SUSPECT:
    i = by_id.get(sid)
    if i is None:
        print(f"seg={sid} 不存在")
        continue
    s = segs[i]
    nxt = segs[i + 1] if i + 1 < len(segs) else None
    print(f"\n=== seg={sid} p{s['page']} {s['kind']} ===")
    print(f"  EN 本段: {s['text']!r}")
    print(f"  ZH 本段: {(tr.get(sid) or {}).get('text')!r}")
    if nxt:
        print(f"  --- 下一段 seg={nxt['id']} p{nxt['page']} {nxt['kind']}")
        print(f"  EN 下段: {nxt['text']!r}")
        print(f"  ZH 下段: {(tr.get(nxt['id']) or {}).get('text')!r}")
        # 判断：下段是否是本句的尾巴
        tail_like = (nxt["text"] or "").strip()[:1].islower()
        print(f"  → 下段以小写开头: {tail_like}")
    else:
        print("  （没有下一段）")
con.close()
