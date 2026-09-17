"""Lead: measure where /api/diff spends its time (engine vs HTTP serialization)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402
from versions import differ  # noqa: E402

con = cdb.connect()

for label in ("cold-ish", "second"):
    t0 = time.perf_counter()
    vd = differ.diff_versions(con, 1, 1, 2)
    dt = time.perf_counter() - t0
    n = len(getattr(vd, "changes", []) or [])
    kinds = {}
    for ch in vd.changes:
        k = str(getattr(ch, "kind", ""))
        kinds[k] = kinds.get(k, 0) + 1
    print(f"diff_versions ({label}): {dt*1000:.0f} ms, changes={n}, kinds={kinds}")

# how expensive is _text_of per change?
t0 = time.perf_counter()
cnt = 0
for ch in vd.changes:
    sid = getattr(ch, "old_segment_id", None)
    if sid:
        store.get_segment(con, sid)
        cnt += 1
    sid = getattr(ch, "new_segment_id", None)
    if sid:
        store.get_segment(con, sid)
        cnt += 1
dt = time.perf_counter() - t0
print(f"per-change get_segment x{cnt}: {dt*1000:.0f} ms  ({dt/max(1,cnt)*1e6:.0f} us each)")

# char_diffs weight
tot = sum(len(getattr(ch, "char_diffs", None) or []) for ch in vd.changes)
print(f"total char_diff ops across all changes: {tot}")

con.close()
