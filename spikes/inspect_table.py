"""Lead: inspect segmentation of a table page to check table structure fidelity."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

VID = int(sys.argv[1]) if len(sys.argv) > 1 else 2
PAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 25

con = cdb.connect()
segs = [s for s in store.segments_of_version(con, VID) if s["page"] == PAGE]
print(f"version {VID} page {PAGE}: {len(segs)} segments\n")
for s in segs:
    bb = s["bbox"]
    print(f"{s['kind']:11s} ord={s['order_index']:5d} "
          f"bbox=[{bb[0]:6.1f},{bb[1]:6.1f},{bb[2]:6.1f},{bb[3]:6.1f}] "
          f"style={json.dumps(s.get('style'), ensure_ascii=False)}")
    print(f"    lines={len(s.get('line_boxes') or [])} chars={len(s['text'])}")
    print(f"    {s['text'][:160]!r}")

# Also: is there any segment whose text contains many spaces (collapsed table row)?
print("\n=== segments whose text looks like a collapsed table row (>=4 spaces) ===")
for s in store.segments_of_version(con, VID):
    if s["role"] != "body":
        continue
    if s["text"].count("  ") >= 2 and len(s["text"]) < 400:
        print(f"  p{s['page']} {s['kind']:10s} {s['text'][:140]!r}")
con.close()
