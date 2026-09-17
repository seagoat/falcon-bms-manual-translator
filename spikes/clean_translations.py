"""Lead: delete the 22 contaminated translations so the engine re-translates them."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb  # noqa: E402

con = cdb.connect()
before = con.execute("SELECT COUNT(*) FROM translations").fetchone()[0]

# 1. mock placeholders
r1 = con.execute("DELETE FROM translations WHERE text LIKE '%〔%'").rowcount
# 2. ordinal-suffix contamination: a translation that is exactly the Korean word
r2 = con.execute("DELETE FROM translations WHERE text='韩国' OR text='한국'").rowcount
# 3. failed rows (empty text) so they are retried
r3 = con.execute("DELETE FROM translations WHERE status='failed' AND (text IS NULL OR text='')").rowcount
con.commit()

after = con.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
print(f"deleted: placeholders={r1}  '韩国'={r2}  failed-empty={r3}")
print(f"translations {before} -> {after}")

print("\n=== segments to retry (should now show as untranslated) ===")
for sid in (65927, 65928, 65929, 66210, 68056, 68059, 68065, 69905, 70136, 70139):
    row = con.execute("SELECT s.id, s.page, s.role, s.text, t.text zh, t.status "
                      "FROM segments s LEFT JOIN translations t ON t.segment_id=s.id "
                      "WHERE s.id=?", (sid,)).fetchone()
    if row:
        print(f"  seg={row['id']} p{row['page']} {row['role']:7s} "
              f"zh={row['zh']!r} status={row['status']!r}")
        print(f"     EN: {row['text'][:90]!r}")
con.close()
