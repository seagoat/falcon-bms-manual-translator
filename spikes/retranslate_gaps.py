"""Lead: re-translate the 44 remaining segments directly via the engine."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import config  # noqa: E402
from core import db as cdb  # noqa: E402
from translate import engine  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ids = json.load(open(os.path.join(ROOT, "data", "derived", "_retranslate_ids.json"),
                    encoding="utf-8"))
print(f"translating {len(ids)} segments (version 2) ...")

con = cdb.connect()
res = engine.translate_segments(con, doc_id=1, version_id=2, segment_ids=ids,
                                provider="deepseek", concurrency=4)
con.commit()
con.close()
print(json.dumps(res, ensure_ascii=False, indent=1))

con = cdb.connect()
print("\n=== results ===")
for sid in ids:
    r = con.execute("SELECT s.page, s.kind, s.text en, t.text zh, t.status "
                    "FROM segments s LEFT JOIN translations t ON t.segment_id=s.id "
                    "WHERE s.id=?", (sid,)).fetchone()
    if r:
        print(f"  p{r['page']:3d} {r['kind']:11s} {r['en'][:48]!r:52s} -> {r['zh']!r} [{r['status']}]")
tot = con.execute("SELECT COUNT(*) FROM segments s LEFT JOIN translations t "
                  "ON t.segment_id=s.id WHERE s.version_id=2 AND s.role='body' "
                  "AND (t.id IS NULL OR trim(t.text)='')").fetchone()[0]
print(f"\nv2 body segments still untranslated: {tot}")
con.close()
