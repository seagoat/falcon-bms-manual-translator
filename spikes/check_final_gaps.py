"""Lead: verify the 'th' contamination is gone and survey untranslated English."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z]{3,}")

con = cdb.connect()
print("=== 1. the 7 ordinal-suffix segments (was 'th' -> '韩国') ===")
for sid in (66210, 68056, 68059, 68065, 69905, 70136, 70139):
    r = con.execute("SELECT s.id,s.page,s.text,t.text zh,t.status,t.provider "
                    "FROM segments s LEFT JOIN translations t ON t.segment_id=s.id "
                    "WHERE s.id=?", (sid,)).fetchone()
    if r:
        print(f"  seg={r['id']} p{r['page']:3d} en={r['text']!r:8s} zh={r['zh']!r:8s} "
              f"status={r['status']} provider={r['provider']}")

print("\n=== 2. cache rows that could still poison short suffixes ===")
for r in con.execute("SELECT fingerprint, provider, text FROM translation_cache "
                     "WHERE length(text) < 12 ORDER BY length(text) LIMIT 20").fetchall():
    print(f"  {r['fingerprint'][:12]} {r['provider']:9s} {r['text']!r}")

print("\n=== 3. body translations with NO Chinese (candidates for real misses) ===")
rows = con.execute(
    "SELECT s.id, s.page, s.kind, s.text, t.text zh, t.status "
    "FROM translations t JOIN segments s ON s.id=t.segment_id "
    "WHERE s.role='body' AND s.version_id=2 AND t.text NOT GLOB '*[一-龥]*' "
    "ORDER BY s.page").fetchall()
print(f"  total: {len(rows)}")
buckets = {"short_code": [], "sentence_like": [], "other": []}
for r in rows:
    en = (r["text"] or "").strip()
    words = LATIN.findall(en)
    if len(en) < 20 and len(words) <= 2:
        buckets["short_code"].append(r)
    elif len(words) >= 4 and len(en) >= 25:
        buckets["sentence_like"].append(r)
    else:
        buckets["other"].append(r)
print(f"  short codes / panel silkscreen: {len(buckets['short_code'])}  (expected, fine)")
print(f"  sentence-like (REAL MISSES):    {len(buckets['sentence_like'])}")
print(f"  other:                          {len(buckets['other'])}")
print("\n  --- sentence-like misses (need retranslation) ---")
for r in buckets["sentence_like"][:40]:
    print(f"    p{r['page']:3d} seg={r['id']} {r['status']:8s} {r['text'][:100]!r}")
print("\n  --- other (sample) ---")
for r in buckets["other"][:20]:
    print(f"    p{r['page']:3d} seg={r['id']} {r['status']:8s} {r['text'][:100]!r}")

print("\n=== 4. jobs table ===")
for r in con.execute("SELECT job_id, kind, status, progress, total, message "
                     "FROM jobs ORDER BY job_id").fetchall():
    print(f"  #{r['job_id']} {r['kind']:10s} {r['status']:9s} {r['progress']}/{r['total']} "
          f"{str(r['message'])[:50]}")
con.close()
