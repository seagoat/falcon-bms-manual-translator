"""Lead: repair the 7 '韩国' translations and purge the poisoned cache row.

Root cause: translation_cache has a row keyed on fingerprint('th') with value '韩国'.
An earlier (now deleted) fuzzy restore script matched by substring, so segments
containing 'th' ('4th fault:', 'a) Working with ...') inherited '韩国'.
Fix: drop the poisoned cache row AND the derived translations, then re-translate.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, fingerprint as fp  # noqa: E402

BAD = [77441, 79280, 79282, 79287, 81117, 81350, 81351]

con = cdb.connect()
n_cache = con.execute("DELETE FROM translation_cache WHERE fingerprint=?",
                      (fp.fingerprint("th"),)).rowcount
n_tr = con.execute(
    "DELETE FROM translations WHERE segment_id IN (%s)"
    % ",".join("?" * len(BAD)), BAD).rowcount
con.commit()
print(f"purged cache rows={n_cache}  translations={n_tr}")

left = con.execute("SELECT COUNT(*) FROM translations WHERE text='韩国'").fetchone()[0]
print(f"remaining text='韩国' translations: {left}")

# sanity: no other cache row maps a tiny token to a disproportionate value
susp = con.execute(
    "SELECT fingerprint, text FROM translation_cache "
    "WHERE length(text) >= 6 AND length(text) <= 8 AND text GLOB '*[一-龥]*'").fetchall()
print(f"\ncache rows with 6-8 char chinese values (spot check): {len(susp)}")
for r in susp[:12]:
    print(f"  {r['fingerprint'][:12]} -> {r['text']!r}")

print("\n=== translations left to do ===")
tot = con.execute("SELECT COUNT(*) FROM segments s LEFT JOIN translations t "
                  "ON t.segment_id=s.id WHERE s.version_id=2 AND s.role='body' "
                  "AND (t.id IS NULL OR t.text='')").fetchone()[0]
print(f"  untranslated body segments in v2: {tot}")
con.close()
