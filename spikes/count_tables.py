"""Lead: quantify how widespread the 'collapsed table row' segmentation problem is.

A collapsed row shows up as a segment whose line_boxes contain multiple lines sharing
the same baseline (y0 within tolerance) at different x positions.
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

VID = int(sys.argv[1]) if len(sys.argv) > 1 else 2
con = cdb.connect()
segs = [s for s in store.segments_of_version(con, VID) if s["role"] == "body"]
print(f"version {VID}: {len(segs)} body segments")

bad = []
for s in segs:
    lbs = s.get("line_boxes") or []
    if len(lbs) < 2:
        continue
    # group by baseline
    groups = {}
    for lb in lbs:
        key = round(lb[1], 0)
        groups.setdefault(key, []).append(lb)
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    if multi:
        bad.append((s, multi))

print(f"segments with same-baseline multi-line (likely collapsed table row): {len(bad)}")
print(f"  fraction of body segments: {len(bad)/max(1,len(segs))*100:.2f}%")
print(f"  pages affected: {len({s['page'] for s, _ in bad})}")
print(f"  pages affected list (first 40): {sorted({s['page'] for s, _ in bad})[:40]}")

print("\n=== worst 12 examples ===")
bad.sort(key=lambda x: -sum(len(v) for v in x[1].values()))
for s, multi in bad[:12]:
    n = sum(len(v) for v in multi.values())
    print(f"  p{s['page']} ord={s['order_index']} kind={s['kind']:11s} "
          f"baselines={len(multi)} runs={n}")
    print(f"    {s['text'][:150]!r}")

print("\n=== kind distribution of affected segments ===")
print(Counter(s["kind"] for s, _ in bad))
print("\n=== baseline-group size distribution ===")
sizes = Counter()
for s, multi in bad:
    for k, v in multi.items():
        sizes[len(v)] += 1
print(sorted(sizes.items()))
con.close()
