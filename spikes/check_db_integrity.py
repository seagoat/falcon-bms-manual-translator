"""Lead: integrity check of the production DB's real translations.

Verifies:
  1. how many translations exist and whether they contain Chinese
  2. whether each translation actually corresponds to its segment (sanity pairing)
  3. whether the segment layout was regenerated (ids/counts) since my verified run
"""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")

con = cdb.connect()
rows = con.execute("SELECT version_id, COUNT(*) n, MIN(id) lo, MAX(id) hi "
                   "FROM segments GROUP BY version_id ORDER BY version_id").fetchall()
print("segments per version:")
for r in rows:
    print(f"  version {r['version_id']}: n={r['n']} id range [{r['lo']}..{r['hi']}]")

print("\ntranslations per version:")
for r in con.execute("SELECT version_id, COUNT(*) n, MIN(s.id) lo, MAX(s.id) hi "
                     "FROM translations t JOIN segments s ON s.id=t.segment_id "
                     "GROUP BY version_id").fetchall():
    print(f"  version {r['version_id']}: n={r['n']} seg id range [{r['lo']}..{r['hi']}]")

print("\n=== pairing sanity: sample 25 machine/carried translations ===")
bad = 0
total = 0
for r in con.execute(
        "SELECT s.id seg, s.page, s.kind, s.text en, s.canonical_text canon, "
        "       t.text zh, t.status st "
        "FROM translations t JOIN segments s ON s.id=t.segment_id "
        "WHERE s.role='body' AND t.status IN ('machine','carried','reviewed') "
        "AND t.text <> '' ORDER BY RANDOM() LIMIT 25").fetchall():
    total += 1
    en, zh = r["en"], r["zh"]
    has_cjk = bool(CJK.search(zh))
    # a translation that is 5x longer than its source, or a long translation for a
    # very short source, is a strong sign of mismatched pairing.
    suspicious = (len(zh) > max(30, len(en) * 3.5)) or (len(en) <= 12 and len(zh) > 25)
    flag = "  <<< SUSPICIOUS" if suspicious else ""
    if suspicious or not has_cjk:
        bad += 1
    print(f"  seg={r['seg']} p{r['page']} {r['kind']:10s} st={r['st']:8s} "
          f"|en|={len(en)} |zh|={len(zh)} cjk={has_cjk}{flag}")
    print(f"     EN: {en[:80]!r}")
    print(f"     ZH: {zh[:80]!r}")

print(f"\nsuspicious / no-CJK samples: {bad}/{total}")

print("\n=== counts ===")
n_body = con.execute("SELECT COUNT(*) FROM segments WHERE role='body'").fetchone()[0]
n_tr = con.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
n_cjk = con.execute("SELECT COUNT(*) FROM translations WHERE text GLOB '*[一-龥]*'").fetchone()[0]
n_mock = con.execute("SELECT COUNT(*) FROM translations WHERE text LIKE '%〔%'").fetchone()[0]
print(f"  body segments={n_body}  translations={n_tr}  with-CJK={n_cjk}  mock-placeholders={n_mock}")
print("\n  kind distribution:")
for r in con.execute("SELECT kind, COUNT(*) n FROM segments GROUP BY kind ORDER BY n DESC").fetchall():
    print(f"    {r['kind']:12s} {r['n']}")
con.close()
