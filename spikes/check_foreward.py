"""Lead: check what FOREWARD segments exist in TO-1 and what _title_for returns."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

from core import db as cdb, fingerprint as fp, store  # noqa: E402

con = cdb.connect()
segs = store.segments_of_version(con, 2)
tr = store.translations_of_version(con, 2)
print("=== p2 全部段 ===")
for s in [x for x in segs if int(x["page"]) == 2][:8]:
    zh = (tr.get(s["id"]) or {}).get("text") or s["text"]
    print(f"  seg={s['id']} {s['kind']:10s} EN={s['text'][:50]!r}")
    print(f"              ZH={zh[:60]!r}")

print("\n=== fingerprint('FOREWARD') 命中哪些段 ===")
f = fp.fingerprint("FOREWARD")
print(f"  fp={f}")
for s in segs:
    if s["fingerprint"] == f:
        print(f"  seg={s['id']} p{s['page']} {s['kind']} {s['text']!r}")
print("\n=== fingerprint('FOREWORD') ===")
f2 = fp.fingerprint("FOREWORD")
for s in segs:
    if s["fingerprint"] == f2:
        print(f"  seg={s['id']} p{s['page']} {s['kind']} {s['text']!r}")
con.close()
