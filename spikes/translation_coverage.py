"""Lead: translation coverage report per version + pending work."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")
con = cdb.connect()
for v in store.list_versions(con, 1):
    vid = v["version_id"]
    st = store.translation_stats(con, vid)
    rows = con.execute(
        "SELECT t.status st, COUNT(*) n, "
        "       SUM(CASE WHEN t.text GLOB '*[一-龥]*' THEN 1 ELSE 0 END) cjk "
        "FROM translations t JOIN segments s ON s.id=t.segment_id "
        "WHERE s.version_id=? AND s.role='body' GROUP BY t.status", (vid,)).fetchall()
    print(f"v{v['version_no']} (id={vid}) label={v.get('label')} body={st['total']} "
          f"translated={st['translated']}")
    for r in rows:
        print(f"    status={r['st']:9s} n={r['n']:5d}  with_cjk={r['cjk']}")
    miss = con.execute(
        "SELECT COUNT(*) FROM segments s LEFT JOIN translations t ON t.segment_id=s.id "
        "WHERE s.version_id=? AND s.role='body' AND (t.id IS NULL OR t.text='')", (vid,)).fetchone()[0]
    print(f"    still missing/empty: {miss}")
con.close()
