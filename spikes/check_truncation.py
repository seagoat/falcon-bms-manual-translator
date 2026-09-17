"""Lead: examine the truncated legal-notice translation on TO-34 p2."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("MT_DATA_DIR", "data/_to_probe")

from core import db as cdb, store  # noqa: E402

con = cdb.connect()
print("=== TO-34 第 2 页全部段与译文 ===")
tr = store.translations_of_version(con, 1)
for s in store.segments_of_version(con, 1, page=2):
    t = tr.get(s["id"])
    print(f"seg={s['id']} {s['kind']:10s} {s['role']:6s} |en|={len(s['text'])}")
    print(f"   EN: {s['text'][:220]!r}")
    print(f"   ZH: {(t['text'] if t else None)!r}")
    print()

print("=== 全书：译文比原文短很多的 machine 段（可能被截断）===")
rows = []
for t in tr.values():
    if t["status"] != "machine":
        continue
    s = store.get_segment(con, t["segment_id"])
    if not s:
        continue
    en, zh = s["text"].strip(), (t["text"] or "").strip()
    if len(en) < 40:
        continue
    # 中文正常约为英文 0.2-0.5 倍字符数；低于 0.08 就很可疑
    r = len(zh) / max(1, len(en))
    if r < 0.08:
        rows.append((r, s, zh))
rows.sort()
print(f"  可疑截断: {len(rows)} 条")
for r, s, zh in rows[:15]:
    print(f"  p{s['page']} ratio={r:.3f} |en|={len(s['text'])} |zh|={len(zh)}")
    print(f"     EN: {s['text'][:140]!r}")
    print(f"     ZH: {zh[:140]!r}")

print("\n=== 全书：map(ing) 检查是否有系统性问题 ===")
short = sum(1 for t in tr.values()
            if t["status"] == "machine"
            and (lambda s: s and len(s["text"]) >= 40
                 and len((t["text"] or "").strip()) / max(1, len(s["text"])) < 0.08)
            (store.get_segment(con, t["segment_id"])))
print(f"  短于原文 8% 的长段: {short} / {len(tr)} = {short/len(tr)*100:.1f}%")
con.close()
