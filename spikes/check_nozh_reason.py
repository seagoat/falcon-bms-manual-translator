"""Lead: two checks for the distribution question.

1) Are 'no-Chinese' translations intentional (model codes / abbreviations that must
   stay English) or real misses?
2) Re-running `translate` on a fresh copy: does it reuse existing translations
   (0 API calls) instead of paying again?
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from collections import Counter

from core import db as cdb  # noqa: E402

con = cdb.connect()
rows = con.execute(
    "SELECT s.id, s.page, s.kind, s.text en, t.text zh, t.status "
    "FROM translations t JOIN segments s ON s.id=t.segment_id "
    "WHERE s.version_id=2 AND s.role='body' AND t.text NOT GLOB '*[一-龥]*' "
    "ORDER BY s.page").fetchall()
print(f"=== v2 body 译文中『不含中文』的条数: {len(rows)} ===")
CAT = {
    "全大写型号/缩写": re.compile(r"^[A-Z0-9][A-Z0-9 /&._\-()%°]*$"),
    "数字/量纲": re.compile(r"^[0-9.,%°/\-\s]+$"),
    "中英混排(含拉丁技术词)": re.compile(r"[A-Za-z]"),
}
buckets = Counter()
samples = {}
for r in rows:
    en, zh = (r["en"] or "").strip(), (r["zh"] or "").strip()
    if CAT["全大写型号/缩写"].match(zh):
        b = "全大写型号/缩写"
    elif CAT["数字/量纲"].match(zh):
        b = "数字/量纲"
    elif CAT["中英混排(含拉丁技术词)"].search(zh):
        b = "中英混排(技术词保留)"
    else:
        b = "其它(需人工看)"
    buckets[b] += 1
    samples.setdefault(b, []).append((r["page"], en, zh))
for b, n in buckets.most_common():
    print(f"  {b:24s} {n}")
    for pg, en, zh in samples[b][:6]:
        print(f"      p{pg:3d} en={en[:40]!r} zh={zh[:40]!r}")
con.close()

print("\n=== 二次运行是否会重复花钱？（离线 mock 也能验证复用逻辑）===")
print("  （下一步用 CLI 在解压副本上跑，见命令输出）")
