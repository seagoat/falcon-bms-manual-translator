"""Lead: verify diff correctness + translation carry-over / seeding chain."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import config  # noqa: E402
from core import db as cdb, store  # noqa: E402
from versions import differ  # noqa: E402

con = cdb.connect()
vd = differ.diff_versions(con, 1, 1, 2)
print("=== diff 1->2 counts:", vd.counts)
print()
for kind in ("modified", "added", "removed", "moved", "reordered", "split", "merged"):
    items = [c for c in vd.changes
             if (c.kind.value if hasattr(c.kind, "value") else str(c.kind)) == kind]
    if not items:
        continue
    print(f"--- {kind} ({len(items)})")
    for c in items:
        print(f"  ratio={c.ratio:.3f} old_page={c.old_page} new_page={c.new_page} "
              f"summary={c.summary!r}")
        if c.old_segment_id:
            o = store.get_segment(con, c.old_segment_id)
            print(f"    OLD: {o['text'][:150]!r}")
        else:
            print("    OLD: (none)")
        if c.new_segment_id:
            n = store.get_segment(con, c.new_segment_id)
            print(f"    NEW: {n['text'][:150]!r}")
        else:
            print("    NEW: (none)")
        if c.char_diffs:
            rendered = "".join(
                (f"[-{d['text']}-]" if d["op"] == "delete"
                 else f"[+{d['text']}+]" if d["op"] == "insert" else d["text"])
                for d in c.char_diffs)
            print(f"    DIFF: {rendered[:300]!r}")
    print()

print("\n=== translation chain: v1 translations vs v2 for linked segments ===")
trans_v1 = store.translations_of_version(con, 1)
trans_v2 = store.translations_of_version(con, 2)
print(f"v1 translations={len(trans_v1)}  v2 translations={len(trans_v2)}")
links = store.links_for_diff(con, 2)
print(f"links for v2 (incl. removed) = {len(links)}")
sample = 0
for sid, t in list(trans_v2.items())[:200]:
    seg = store.get_segment(con, sid)
    if seg["page"] != 21:
        continue
    link = links.get(sid)
    if not link:
        continue
    old_id = link.get("old_segment_id")
    old_t = trans_v1.get(old_id) if old_id else None
    if not old_t:
        continue
    sample += 1
    if sample > 3:
        break
    print(f"  v2 seg {sid} p{seg['page']} status={t['status']} reuse_of={t.get('reuse_of')}")
    print(f"    EN        : {seg['text'][:110]!r}")
    print(f"    ZH(v2)    : {t['text'][:110]!r}")
    print(f"    ZH(v1 ref): {old_t['text'][:110]!r}")
    print(f"    link      : kind={link.get('change_kind')} ratio={link.get('ratio')}")

print("\n=== status distribution in v2 ===")
st = {}
for t in trans_v2.values():
    st[t["status"]] = st.get(t["status"], 0) + 1
print(st)
con.close()
