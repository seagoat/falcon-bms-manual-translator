"""Lead: inspect actual Chinese translations produced by the real end-to-end run."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

con = cdb.connect()
VID = int(sys.argv[1]) if len(sys.argv) > 1 else 2
PAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 21

trans = store.translations_of_version(con, VID)
segs = [s for s in store.segments_of_version(con, VID) if s["page"] == PAGE]
print(f"version {VID} page {PAGE}: {len(segs)} segments, "
      f"{sum(1 for s in segs if s['id'] in trans)} translated\n")

for s in segs:
    t = trans.get(s["id"])
    if not t or not (t.get("text") or "").strip():
        continue
    zh = t["text"]
    hl = [l for l in s.get("line_boxes") or []]
    print(f"[{s['kind']:10s}] seg={s['id']} status={t['status']:8s} rev={t.get('revision')} "
          f"bbox_h={s['bbox'][3]-s['bbox'][1]:5.1f} lines={len(hl)} "
          f"|zh|={len(zh)} |en|={len(s['text'])}")
    print(f"   EN: {s['text'][:150]}")
    print(f"   ZH: {zh[:150]}")
    print()

print("=== status totals for version", VID, "===")
cnt = {}
for t in trans.values():
    cnt[t["status"]] = cnt.get(t["status"], 0) + 1
print(cnt)
print("total:", len(trans))
con.close()
