"""Lead: recover a PDF outline for the CN/bilingual exports.

Problem: `origin/BMS-Training-Manual_v2_demo.pdf` has **no** bookmarks (it was rebuilt
by pymupdf for the demo), so `_finish()` copies an empty TOC. The ORIGINAL v1 manual
has 231 bookmarks. We can:
  1. take v1's bookmarks (level/title/page),
  2. locate the matching segment in the *current* version (v2) to get the right page,
  3. use the segment's Chinese translation as the bookmark title,
  4. emit a TOC for the exported CN / bilingual PDF.

This script measures the mapping quality before wiring it into pdfout.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

from core import db as cdb, fingerprint as fp, store  # noqa: E402

V1 = "origin/BMS-Training-Manual.pdf"
V2 = "origin/BMS-Training-Manual_v2_demo.pdf"

d1 = pymupdf.open(V1)
d2 = pymupdf.open(V2)
toc1 = d1.get_toc() or []
toc2 = d2.get_toc() or []
print(f"v1 书签: {len(toc1)}   v2 书签: {len(toc2)}")
d1.close()
d2.close()

con = cdb.connect()
segs1 = store.segments_of_version(con, 1)
segs2 = store.segments_of_version(con, 2)
tr2 = store.translations_of_version(con, 2)
print(f"v1 段: {len(segs1)}  v2 段: {len(segs2)}  译文: {len(tr2)}")

# 建立 v1 页 -> 该页的段（用于把书签页定位到段）
by_page1 = {}
for s in segs1:
    by_page1.setdefault(int(s["page"]), []).append(s)

# v2 的 fingerprint -> 段（用于跨版本定位）
fp2 = {}
for s in segs2:
    fp2.setdefault(s["fingerprint"], []).append(s)

matched = moved = lost = 0
rows = []
for level, title, page in toc1:
    cands = by_page1.get(int(page), [])
    tnorm = fp.normalize(title).casefold()
    hit = None
    # 优先：标题与段文本一致
    for s in cands:
        if fp.normalize(s["text"]).casefold() == tnorm:
            hit = s
            break
    # 其次：段文本以标题开头
    if hit is None:
        for s in cands:
            if fp.normalize(s["text"]).casefold().startswith(tnorm) and len(tnorm) > 3:
                hit = s
                break
        # 再其次：标题包含段文本（书签常常合并了编号与标题）
    if hit is None:
        best = None
        for s in cands:
            n = fp.normalize(s["text"]).casefold()
            if len(n) >= 4 and n in tnorm:
                if best is None or len(n) > len(fp.normalize(best["text"])):
                    best = s
        hit = best

    if hit is None:
        lost += 1
        rows.append((level, title, page, None, None, None))
        continue
    # 到 v2 找同 fingerprint 的段
    n2 = fp2.get(hit["fingerprint"], [])
    if not n2:
        # 退而求其次：同页同序
        n2 = [s for s in segs2 if int(s["page"]) == int(hit["page"])]
    if not n2:
        lost += 1
        rows.append((level, title, page, None, None, hit["text"]))
        continue
    tgt = n2[0]
    zh = (tr2.get(tgt["id"]) or {}).get("text") or tgt["text"]
    if int(tgt["page"]) != int(page):
        moved += 1
    else:
        matched += 1
    rows.append((level, title, page, int(tgt["page"]), zh, hit["text"]))

print(f"\n=== 映射结果 ===")
print(f"  同页命中: {matched}")
print(f"  跨页移动: {moved}")
print(f"  未能定位: {lost}")
print(f"  合计    : {len(toc1)}")
print(f"\n=== 前 20 条 ===")
for level, title, page, np_, zh, hit in rows[:20]:
    flag = "OK " if np_ else "MISS"
    print(f"  {flag} L{level} p{page:3d}->{np_ if np_ else '--'}  {title[:42]:44s} | {str(zh)[:40]}")

print(f"\n=== 未定位的条目 ===")
for level, title, page, np_, zh, hit in rows:
    if np_ is None:
        print(f"  L{level} p{page:3d} {title[:70]!r}")
con.close()
