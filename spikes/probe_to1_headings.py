"""Lead: why does TO-1's outline map poorly while TO-34's is fine?

Look at the actual segments near a TO-1 bookmark page to see how headings are classified
and whether the heading text carries the section number.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["MT_DATA_DIR"] = "data/_to_probe"

import pymupdf  # noqa: E402

from core import db as cdb, store  # noqa: E402

con = cdb.connect()
print("=== TO-1 (version_id=2) 在书签页附近的段 ===")
for pno in (19, 20, 24, 25):
    segs = store.segments_of_version(con, 2, page=pno)
    print(f"\n--- p{pno}: {len(segs)} 段")
    for s in segs[:10]:
        print(f"   {s['kind']:10s} {s['role']:6s} size={(s.get('style') or {}).get('size')} "
              f"{s['text'][:70]!r}")

print("\n=== TO-34 (version_id=1) 对照 ===")
for pno in (36, 37):
    segs = store.segments_of_version(con, 1, page=pno)
    print(f"\n--- p{pno}: {len(segs)} 段")
    for s in segs[:8]:
        print(f"   {s['kind']:10s} {s['role']:6s} size={(s.get('style') or {}).get('size')} "
              f"{s['text'][:70]!r}")

# heading 数量对比
for vid, label in ((2, "TO-1"), (1, "TO-34")):
    segs = store.segments_of_version(con, vid)
    from collections import Counter
    print(f"\n{label} kind 分布: {dict(Counter(s['kind'] for s in segs))}")
con.close()

# 书签页 19 的原始文本（从 PDF 直接看）
d = pymupdf.open("origin/TO 1F-16CMAM-1 BMS.pdf")
print("\n=== PDF p19 原始文本前 600 字 ===")
print(d[18].get_text()[:600])
print("\n=== PDF p24 原始文本前 400 字 ===")
print(d[23].get_text()[:400])
d.close()
