"""Lead: fix the recurring 'th' -> '韩国' contamination and add a permanent guard.

The poisoned value lives in translation_cache under fingerprint('th'). Every restore
that copies by fingerprint re-injects it. Purge cache + translations, then re-translate.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, fingerprint as fp  # noqa: E402

con = cdb.connect()
rows = con.execute("SELECT s.id, s.page, s.text en, t.text zh, t.status FROM translations t "
                   "JOIN segments s ON s.id=t.segment_id WHERE t.text='韩国'").fetchall()
print(f"=== affected translations: {len(rows)} ===")
bad_ids = []
for r in rows:
    bad_ids.append(r["id"])
    print(f"  seg={r['id']} p{r['page']} en={r['en'][:70]!r} zh={r['zh']!r} status={r['status']}")
print("  bad segment_ids:", bad_ids)

# purge the poisoned cache row and every cache row that maps a short token to a
# short chinese value derived from it (defence in depth)
targets = [fp.fingerprint("th")]
n_cache = 0
for f in targets:
    n_cache += con.execute("DELETE FROM translation_cache WHERE fingerprint=?", (f,)).rowcount
# also drop cache rows whose text is exactly the poisoned value but whose fingerprint
# is not the natural one for '韩国' (i.e. contamination, not a legitimate translation)
n_cache += con.execute(
    "DELETE FROM translation_cache WHERE text='韩国' AND fingerprint != ?",
    (fp.fingerprint("韩国"),)).rowcount
con.commit()
print(f"\npurged cache rows: {n_cache}")

# the legitimate 'South Korea' translation is 韩国 -> keep it, but only for segments
# whose source actually means Korea. Delete only the mismatched ones.
deleted = 0
for sid in bad_ids:
    seg = con.execute("SELECT text FROM segments WHERE id=?", (sid,)).fetchone()
    en = (seg["text"] or "").lower()
    # accept 韩国 only when the source is genuinely a short Korea reference;
    # a long sentence collapsing to 2 characters is contamination by definition.
    if "korea" not in en or len(en) > 24:
        con.execute("DELETE FROM translations WHERE segment_id=?", (sid,))
        deleted += 1
con.commit()
print(f"deleted mismatched translations: {deleted}")
left = con.execute("SELECT COUNT(*) FROM translations WHERE text='韩国'").fetchone()[0]
print(f"remaining '韩国' translations (should all be real 'Korea' sources): {left}")
for r in con.execute("SELECT s.id,s.page,s.text en,s.role FROM translations t "
                     "JOIN segments s ON s.id=t.segment_id WHERE t.text='韩国'").fetchall():
    print(f"  seg={r['id']} p{r['page']} role={r['role']} en={r['en'][:70]!r}")

need = con.execute("SELECT COUNT(*) FROM segments s LEFT JOIN translations t "
                   "ON t.segment_id=s.id WHERE s.version_id=2 AND s.role='body' "
                   "AND (t.id IS NULL OR trim(t.text)='')").fetchone()[0]
print(f"\nv2 body segments now untranslated: {need}")
con.close()
