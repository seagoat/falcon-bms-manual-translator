"""Lead: purge contaminated translation_cache rows, then list what must be re-translated.

Contamination sources (found by verifier, confirmed here):
  A. mock provider placeholders that survived the DB rebuild in translation_cache
     and are re-served as CARRIED -> '〔TRAINING〕 〔MANUAL〕'
  B. a polluting cache row for the ordinal suffix 'th' -> '韩国'
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb  # noqa: E402

con = cdb.connect()
print("=== translation_cache profile ===")
for r in con.execute("SELECT provider, model, COUNT(*) n, "
                     " SUM(CASE WHEN text LIKE '%〔%' THEN 1 ELSE 0 END) placeholder "
                     "FROM translation_cache GROUP BY provider, model ORDER BY n DESC"):
    print(f"  provider={r['provider']!r:12s} model={r['model']!r:20s} n={r['n']:6d} "
          f"placeholder={r['placeholder']}")

print("\n=== cache rows that are mock or contain 〔〕 ===")
rows = con.execute(
    "SELECT fingerprint, provider, model, text FROM translation_cache "
    "WHERE provider='mock' OR text LIKE '%〔%'").fetchall()
for r in rows:
    print(f"  fp={r['fingerprint'][:12]} provider={r['provider']!r} text={r['text'][:70]!r}")

n_mock = con.execute("SELECT COUNT(*) FROM translation_cache WHERE provider='mock'").fetchone()[0]
n_ph = con.execute("SELECT COUNT(*) FROM translation_cache WHERE text LIKE '%〔%'").fetchone()[0]
print(f"\nwould delete: provider='mock' -> {n_mock}, text has 〔〕 -> {n_ph}")

# the ordinal-suffix cache row: short english -> long chinese contamination
bad_short = con.execute(
    "SELECT fingerprint, provider, model, text FROM translation_cache "
    "WHERE length(text) > 12 AND length(text) < 200 "
    "AND (text LIKE '%韩国%' OR text LIKE '%AN\uff27%')").fetchall()
print("\n=== suspicious 'th'/ordinal cache rows ===")
for r in bad_short:
    print(f"  fp={r['fingerprint'][:12]} provider={r['provider']!r} text={r['text'][:80]!r}")

con.execute("DELETE FROM translation_cache WHERE provider='mock' OR text LIKE '%〔%'")
con.commit()
left = con.execute("SELECT COUNT(*) FROM translation_cache").fetchone()[0]
print(f"\n✅ deleted; translation_cache now {left} rows")

print("\n=== affected translations (to re-translate) ===")
CJK_BAD = "text LIKE '%〔%'"
n1 = con.execute("SELECT COUNT(*) FROM translations WHERE " + CJK_BAD).fetchone()[0]
print(f"  translations with 〔〕 placeholders: {n1}")
for r in con.execute("SELECT id, segment_id, text FROM translations WHERE " + CJK_BAD).fetchall():
    print(f"    translation id={r['id']} seg={r['segment_id']} text={r['text'][:70]!r}")
n2 = con.execute("SELECT COUNT(*) FROM translations WHERE text='韩国'").fetchone()[0]
print(f"  translations with text='韩国': {n2}")
for r in con.execute("SELECT id, segment_id, text FROM translations WHERE text='韩国'").fetchall():
    print(f"    translation id={r['id']} seg={r['segment_id']}")
n3 = con.execute("SELECT COUNT(*) FROM translations WHERE status='failed'").fetchone()[0]
print(f"  translations with status='failed': {n3}")
con.close()
