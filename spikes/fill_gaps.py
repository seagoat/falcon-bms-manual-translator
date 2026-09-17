"""Lead: fill translation gaps in the frozen DB using the pre-rebuild backup.

Strategy (exact-first, never fuzzy):
  1. copy data/app.db -> review/tmp/app_snapshot_pre_lead.db (safety)
  2. load data/derived/translations_backup_20260917_111402.json (5650 rows, has fingerprint)
  3. for every v2 body segment with no translation, look up by EXACT fingerprint
  4. also try exact canonical_text match (fingerprint can differ if normalize changed)
  5. report what is still missing (those get re-translated via the API)
"""
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, fingerprint as fp, store  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, "review", "tmp", "app_snapshot_pre_lead.db")
os.makedirs(os.path.dirname(SNAP), exist_ok=True)
if not os.path.exists(SNAP):
    shutil.copy2(cdb.connect and os.path.join(ROOT, "data", "app.db"), SNAP)
    print("snapshot ->", SNAP)
else:
    print("snapshot already exists ->", SNAP)

BK = os.path.join(ROOT, "data", "derived", "translations_backup_20260917_111402.json")
rows = json.load(open(BK, encoding="utf-8"))
print(f"backup rows: {len(rows)}")

by_fp = {}
by_canon = {}
for r in rows:
    if not (r.get("text") or "").strip():
        continue
    if r.get("fingerprint"):
        by_fp.setdefault(r["fingerprint"], r)
    ct = (r.get("canonical_text") or "").strip()
    if ct:
        by_canon.setdefault(fp.normalize(ct).casefold(), r)
print(f"  usable: by_fingerprint={len(by_fp)} by_canonical={len(by_canon)}")

con = cdb.connect()
missing = con.execute(
    "SELECT s.id, s.page, s.role, s.text, s.canonical_text, s.fingerprint, s.kind "
    "FROM segments s LEFT JOIN translations t ON t.segment_id=s.id "
    "WHERE s.version_id=2 AND s.role='body' AND (t.id IS NULL OR trim(t.text)='')").fetchall()
print(f"\nv2 body segments without translation: {len(missing)}")

filled_fp = filled_canon = 0
still = []
for m in missing:
    hit = by_fp.get(m["fingerprint"])
    how = "fp"
    if not hit:
        key = fp.normalize(m["canonical_text"] or "").casefold()
        hit = by_canon.get(key)
        how = "canonical"
    if not hit:
        still.append(m)
        continue
    store.upsert_translation(con, segment_id=m["id"], text=hit["text"],
                             status="carried", provider=hit.get("provider") or "deepseek",
                             model=hit.get("model") or "", confidence=float(hit.get("confidence") or 0.0))
    if how == "fp":
        filled_fp += 1
    else:
        filled_canon += 1
con.commit()
print(f"restored from backup: by_fingerprint={filled_fp}  by_canonical={filled_canon}")
print(f"still missing: {len(still)}")

if still:
    print("\n=== still missing (need API re-translation) ===")
    from collections import Counter
    print("  by kind:", dict(Counter(m["kind"] for m in still)))
    print("  by page band:", dict(Counter((m["page"] // 50) * 50 for m in still)))
    for m in still[:25]:
        print(f"    p{m['page']:3d} {m['kind']:11s} en={m['text'][:60]!r}")

need_ids = [m["id"] for m in still]
open(os.path.join(ROOT, "data", "derived", "_retranslate_ids.json"), "w",
     encoding="utf-8").write(json.dumps(need_ids))
print(f"\nwrote {len(need_ids)} ids -> data/derived/_retranslate_ids.json")
con.close()
