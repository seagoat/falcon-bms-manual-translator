"""Lead: diagnose why 'th'-bearing segments resolve to '韩国', then repair them."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, fingerprint as fp  # noqa: E402
from translate import glossary  # noqa: E402

BAD = [77441, 79280, 79282, 79287, 81117, 81350, 81351]

con = cdb.connect()
print("=== affected segments + their fp + cache hit ===")
for sid in BAD:
    s = con.execute("SELECT id,page,text,canonical_text,fingerprint FROM segments WHERE id=?",
                    (sid,)).fetchone()
    t = con.execute("SELECT text,status,provider,model,reuse_of FROM translations "
                    "WHERE segment_id=?", (sid,)).fetchone()
    if not s:
        print(f"  seg={sid} NOT FOUND")
        continue
    fpv = s["fingerprint"]
    c = con.execute("SELECT text,provider FROM translation_cache WHERE fingerprint=?",
                    (fpv,)).fetchone()
    print(f"  seg={sid} p{s['page']} fp={fpv[:14]} cache={c['text'] if c else None!r}")
    print(f"     EN  : {s['text'][:80]!r}")
    print(f"     ZH  : {t['text'] if t else None!r} status={t['status'] if t else None} "
          f"provider={t['provider'] if t else None} reuse_of={t['reuse_of'] if t else None}")

print("\n=== is there a cache row whose fingerprint equals the ORDINAL 'th'? ===")
for cand in ("th", "4th", "4th fault:", "a) Working", "South Korea"):
    f = fp.fingerprint(cand)
    c = con.execute("SELECT text,provider FROM translation_cache WHERE fingerprint=?",
                    (f,)).fetchone()
    print(f"  {cand!r:22s} fp={f[:14]} -> {c['text'] if c else None!r}")

print("\n=== what does glossary.apply do to these? ===")
for cand in ("4th fault:", "South Korea and is the home of the 8th Fighter Wing, "
             "also called the \u201cWolf Pack\u201d.",
             "a) Working with the BMS AI JTAC (Joint Terminal Attack Controller)."):
    print(f"  {cand[:60]!r}\n    -> {glossary.apply(cand)!r}")
con.close()
