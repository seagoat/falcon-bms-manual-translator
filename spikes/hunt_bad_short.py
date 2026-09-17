"""Lead: hunt for any remaining nonsense translations of ultra-short segments."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb  # noqa: E402

con = cdb.connect()

print("=== ultra-short body segments (<=6 chars) whose translation looks wrong ===")
rows = con.execute(
    "SELECT s.id, s.page, s.kind, s.text en, t.text zh, t.status "
    "FROM translations t JOIN segments s ON s.id=t.segment_id "
    "WHERE s.role='body' AND s.version_id=2 AND length(trim(s.text)) <= 6 "
    "ORDER BY s.page").fetchall()
print(f"  total ultra-short body segments: {len(rows)}")
bad = []
for r in rows:
    en = (r["en"] or "").strip()
    zh = (r["zh"] or "").strip()
    # a translation much longer than its tiny source is suspect
    if len(zh) > max(4, len(en) * 2 + 2):
        bad.append(r)
print(f"  suspicious (zh much longer than en): {len(bad)}")
for r in bad[:40]:
    print(f"    p{r['page']:3d} seg={r['id']} en={r['en']!r:22s} zh={r['zh']!r:28s} {r['status']}")

print("\n=== all segments whose text is an ordinal suffix ===")
for r in con.execute("SELECT id, page, text, role FROM segments WHERE version_id=2 "
                     "AND trim(lower(text)) IN ('th','st','nd','rd')").fetchall():
    t = con.execute("SELECT text,status FROM translations WHERE segment_id=?", (r["id"],)).fetchone()
    print(f"  seg={r['id']} p{r['page']} role={r['role']} text={r['text']!r} "
          f"zh={t['text'] if t else None!r} status={t['status'] if t else None}")

print("\n=== '韩国' in any short translation (former contamination signature) ===")
for r in con.execute("SELECT s.id, s.page, s.text en, t.text zh FROM translations t "
                     "JOIN segments s ON s.id=t.segment_id "
                     "WHERE t.text='韩国' OR (t.text LIKE '%韩国%' AND length(t.text)<8)").fetchall():
    print(f"  seg={r['id']} p{r['page']} en={r['en']!r} zh={r['zh']!r}")

print("\n=== longest-vs-shortest ratio outliers (zh > 3x en, en >= 8) ===")
rows = con.execute(
    "SELECT s.id, s.page, s.kind, s.text en, t.text zh FROM translations t "
    "JOIN segments s ON s.id=t.segment_id "
    "WHERE s.role='body' AND s.version_id=2 AND length(trim(s.text)) >= 8 "
    "AND length(t.text) > 3 * length(trim(s.text)) + 40").fetchall()
print(f"  outliers: {len(rows)}")
for r in rows[:20]:
    print(f"    p{r['page']:3d} {r['kind']:10s} en={r['en'][:45]!r} zh={r['zh'][:60]!r}")
con.close()
