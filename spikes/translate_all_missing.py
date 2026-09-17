"""Lead: translate whatever is currently missing in v2 (finds the ids itself)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb  # noqa: E402
from translate import engine  # noqa: E402

con = cdb.connect()
ids = [r["id"] for r in con.execute(
    "SELECT s.id FROM segments s LEFT JOIN translations t ON t.segment_id=s.id "
    "WHERE s.version_id=2 AND s.role='body' AND (t.id IS NULL OR trim(t.text)='') "
    "ORDER BY s.order_index").fetchall()]
print(f"currently untranslated in v2: {len(ids)} -> {ids}")

if ids:
    res = engine.translate_segments(con, doc_id=1, version_id=2, segment_ids=ids,
                                    provider="deepseek", concurrency=4)
    con.commit()
    print(json.dumps({k: res[k] for k in ("translated", "carried", "failed", "api_calls")},
                     ensure_ascii=False))
    print("\n=== results ===")
    for sid in ids:
        r = con.execute("SELECT s.page,s.kind,s.text en,t.text zh,t.status "
                        "FROM segments s LEFT JOIN translations t ON t.segment_id=s.id "
                        "WHERE s.id=?", (sid,)).fetchone()
        print(f"  p{r['page']:3d} {r['kind']:11s} {r['en'][:56]!r}")
        print(f"        -> {r['zh']!r} [{r['status']}]")

left = con.execute("SELECT COUNT(*) FROM segments s LEFT JOIN translations t "
                   "ON t.segment_id=s.id WHERE s.version_id=2 AND s.role='body' "
                   "AND (t.id IS NULL OR trim(t.text)='')").fetchone()[0]
kr = con.execute("SELECT COUNT(*) FROM translations WHERE text='韩国'").fetchone()[0]
print(f"\nv2 untranslated: {left}   text='韩国': {kr}")
con.close()
