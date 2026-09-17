"""Lead: assess damage from the over-broad repair_continuation run on TO-1.

Most of the 110 'pairs' were false positives (headings / TOC rows / bullets).
Check whether the translation cache still holds the pre-repair (correct) translations,
which would let us restore them without re-calling the API.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

from core import db as cdb, fingerprint as fp, store  # noqa: E402

SUSPECT_IDS = [11806, 11814, 11818, 11829, 11840, 11841, 11842, 11843]

con = cdb.connect()
for sid in SUSPECT_IDS:
    s = store.get_segment(con, sid)
    if not s:
        continue
    t = con.execute("SELECT text, status FROM translations WHERE segment_id=?",
                    (sid,)).fetchone()
    cache = con.execute("SELECT text, provider, created_at FROM translation_cache "
                        "WHERE fingerprint=?", (s["fingerprint"],)).fetchone()
    print(f"seg={sid} p{s['page']} {s['kind']}")
    print(f"  EN     : {s['text'][:70]!r}")
    print(f"  当前ZH : {(t['text'] if t else None)!r}  [{t['status'] if t else '-'}]")
    print(f"  缓存ZH : {(cache['text'] if cache else None)!r}")
    if cache and t and cache["text"] == t["text"]:
        print("    （缓存 === 当前 → 缓存已被本次修复覆盖）")
    print()

# 统计：缓存里有多少条与当前译文不同（说明缓存是"修复前"的版本）
same = diff = 0
rows = con.execute(
    "SELECT s.fingerprint, t.text FROM translations t JOIN segments s ON s.id=t.segment_id "
    "WHERE s.version_id=2 AND s.role='body'").fetchall()
for r in rows:
    c = con.execute("SELECT text FROM translation_cache WHERE fingerprint=?",
                    (r["fingerprint"],)).fetchone()
    if not c:
        continue
    if c["text"] == r["text"]:
        same += 1
    else:
        diff += 1
print(f"缓存与当前译文：相同 {same}，不同 {diff}")
con.close()
