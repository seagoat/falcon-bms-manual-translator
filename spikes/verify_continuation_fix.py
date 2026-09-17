"""Lead: verify the continuation repair fixed the known corruption and did not
damage ordinary translations."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

from core import db as cdb, store  # noqa: E402

con = cdb.connect()
segs = store.segments_of_version(con, 2)
tr = store.translations_of_version(con, 2)
by = {int(s["id"]): i for i, s in enumerate(segs)}

print("=== 已知损坏的安全警告 p185（修复目标）===")
i = by.get(14398)
if i is not None:
    for j in range(i, i + 2):
        s = segs[j]
        print(f"  seg={s['id']} EN: {s['text']!r}")
        print(f"           ZH: {(tr.get(s['id']) or {}).get('text')!r}")

print("\n=== 抽样检查修复后的 110 对里是否合理（前 8 对）===")
SENT_END = set(".!?。！？:;\"'”’)")
checked = 0
for i in range(len(segs) - 1):
    a, b = segs[i], segs[i + 1]
    ea, eb = (a["text"] or "").rstrip(), (b["text"] or "").strip()
    if not ea or not eb or ea[-1] in SENT_END or len(eb) < 30:
        continue
    tb = tr.get(b["id"])
    if not tb:
        continue
    zb = (tb.get("text") or "").strip()
    if len(zb) >= 30:          # 已修复（原来是 <20）
        ta = tr.get(a["id"])
        za = (ta.get("text") or "").strip() if ta else ""
        print(f"  p{a['page']} seg={a['id']}/{b['id']}")
        print(f"     A.en ...{a['text'][-50:]!r}")
        print(f"     A.zh ...{za[-50:]!r}")
        print(f"     B.en {b['text'][:50]!r}")
        print(f"     B.zh {zb[:50]!r}")
        checked += 1
        if checked >= 8:
            break

print("\n=== 完整性 ===")
tot = con.execute("SELECT COUNT(*) FROM segments WHERE version_id=2 AND role='body'").fetchone()[0]
miss = con.execute(
    "SELECT COUNT(*) FROM segments s LEFT JOIN translations t ON t.segment_id=s.id "
    "WHERE s.version_id=2 AND s.role='body' AND (t.id IS NULL OR trim(t.text)='')").fetchone()[0]
print(f"  body={tot}  未译={miss}  有译文={len(tr)}")
con.close()
