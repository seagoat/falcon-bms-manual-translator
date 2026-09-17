"""Lead: inspect TO-34 translation quality across page types."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("MT_DATA_DIR", "data/_to_probe")

from core import db as cdb, store  # noqa: E402

con = cdb.connect()
tr = store.translations_of_version(con, 1)
print(f"译文总数: {len(tr)}")
cnt = {}
for t in tr.values():
    cnt[t["status"]] = cnt.get(t["status"], 0) + 1
print("状态分布:", cnt)

# 分层抽样：正文段 / 表格单元 / 列表项 / 标题
import collections  # noqa: E402
by_kind = collections.defaultdict(list)
for t in tr.values():
    s = store.get_segment(con, t["segment_id"])
    if s:
        by_kind[s["kind"]].append((s, t))

for kind in ("paragraph", "table_cell", "list_item", "heading"):
    items = by_kind.get(kind, [])
    print(f"\n=== {kind}（{len(items)} 条，抽 4 条）===")
    step = max(1, len(items) // 4)
    for s, t in items[::step][:4]:
        print(f"  p{s['page']} EN: {s['text'][:150]}")
        print(f"       ZH: {t['text'][:150]}")
        print()

# 残留英文（可能漏译）
print("=== 可能漏译（machine 但无中文，且原文 ≥6 字符）===")
bad = []
for t in tr.values():
    if t["status"] != "machine":
        continue
    if any("\u4e00" <= c <= "\u9fff" for c in t["text"]):
        continue
    s = store.get_segment(con, t["segment_id"])
    if s and len(s["text"].strip()) >= 6:
        bad.append((s, t))
print(f"  共 {len(bad)} 条")
for s, t in bad[:12]:
    print(f"  p{s['page']} {s['kind']:10s} {s['text'][:60]!r} -> {t['text'][:60]!r}")
con.close()
