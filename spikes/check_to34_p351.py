"""Lead: check coverage of TO-34 page 351 (the CN pane looked blank in the UI)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

con = cdb.connect()
segs = store.segments_of_version(con, 3, page=351)      # TO-34 version_id=3
tr = store.translations_of_version(con, 3)
print(f"TO-34 p351: {len(segs)} 段")
for s in segs[:20]:
    t = tr.get(s["id"])
    zh = (t or {}).get("text") or ""
    print(f"  seg={s['id']} {s['kind']:10s} {s['role']:6s} "
          f"EN={s['text'][:44]!r}")
    print(f"        ZH={zh[:44]!r} [{(t or {}).get('status','-')}]")

print("\n=== TO-34 全书：body 段里有译文的页分布 ===")
from collections import Counter  # noqa: E402
allsegs = [s for s in store.segments_of_version(con, 3) if s["role"] == "body"]
have = sum(1 for s in allsegs if (tr.get(s["id"]) or {}).get("text"))
print(f"  body={len(allsegs)} 有译文={have} 未译={len(allsegs)-have}")

# 336..360 区间的覆盖
for pno in range(336, 361):
    ps = [s for s in allsegs if int(s["page"]) == pno]
    h = sum(1 for s in ps if (tr.get(s["id"]) or {}).get("text"))
    if ps:
        print(f"  p{pno}: {h}/{len(ps)}")
con.close()
