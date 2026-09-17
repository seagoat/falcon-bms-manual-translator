"""Lead: final end-to-end consistency check on the frozen DB.

Confirms the exported PDFs match the DB state (no stale artifacts) and that the
version-tracking story still holds on the frozen data.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402
from core import db as cdb, store  # noqa: E402
from versions import differ  # noqa: E402

con = cdb.connect()

print("=== 1. frozen DB state ===")
for v in store.list_versions(con, 1):
    st = store.translation_stats(con, v["version_id"])
    print(f"  v{v['version_no']} id={v['version_id']} current={v['is_current']} "
          f"pages={v['page_count']} body={st['total']} translated={st['translated']} "
          f"{st['by_status']}")

print("\n=== 2. version diff on frozen data ===")
vd = differ.diff_versions(con, 1, 1, 2)
print("  counts:", {k: v for k, v in vd.counts.items() if isinstance(v, int)})

print("\n=== 3. exported PDFs vs DB ===")
for path in ("data/derived/full_cn.pdf", "data/derived/full_bi.pdf"):
    if not os.path.exists(path):
        print(f"  {path} MISSING")
        continue
    d = pymupdf.open(path)
    # sample: does page 21 of the CN pdf carry the DB translation for a known segment?
    txt = d[20].get_text()
    seg = con.execute(
        "SELECT s.text en, t.text zh FROM segments s JOIN translations t "
        "ON t.segment_id=s.id WHERE s.version_id=2 AND s.page=21 "
        "AND s.role='body' AND length(trim(s.text))>40 ORDER BY s.order_index LIMIT 1").fetchone()
    ok = seg and seg["zh"][:12] in txt
    print(f"  {os.path.basename(path):16s} pages={d.page_count}  "
          f"p21 contains DB translation snippet: {bool(ok)}")
    if seg:
        print(f"      DB  zh: {seg['zh'][:60]!r}")
    d.close()

print("\n=== 4. no residual placeholders anywhere ===")
for tbl, col in (("translations", "text"), ("translation_cache", "text")):
    n = con.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {col} LIKE '%〔%'").fetchone()[0]
    print(f"  {tbl}: rows containing 〔〕 = {n}")
n_kr = con.execute("SELECT COUNT(*) FROM translations WHERE text='韩国'").fetchone()[0]
print(f"  translations text='韩国' = {n_kr}")

print("\n=== 5. jobs all terminal ===")
for r in con.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status"):
    print(f"  {r['status']}: {r['n']}")

print("\n=== 6. render snapshots present ===")
for r in con.execute("SELECT kind, path, bytes FROM render_snapshots "
                     "ORDER BY id DESC LIMIT 6"):
    print(f"  {r['kind']:10s} {os.path.basename(r['path'] or '')} {r['bytes'] or 0} B")
con.close()
