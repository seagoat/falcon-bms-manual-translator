"""Lead: find out why set_toc silently fails."""
import os
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf

from core import db as cdb, store
from render import pdfout

con = cdb.connect()
toc = pdfout.build_cn_outline(con, 2, prev_pdf="origin/BMS-Training-Manual.pdf")
con.close()
print(f"TOC: {len(toc)} 条")

# 1) level 序列是否合法（pymupdf 要求第一条为 1，且不能从 1 直接跳到 3+）
print("\n=== level 序列检查 ===")
bad = []
for i, (lv, t, pg) in enumerate(toc):
    if i == 0 and lv != 1:
        bad.append((i, "首条不是 level 1", lv, t))
    if i > 0:
        prev = toc[i - 1][0]
        if lv > prev + 1:
            bad.append((i, f"从 L{prev} 跳到 L{lv}", lv, t))
print(f"  问题 {len(bad)} 处")
for i, why, lv, t in bad[:10]:
    print(f"    #{i} {why}: {t[:50]}")

# 2) 页号是否单调（pymupdf 通常容忍，但报错时值得看）
print("\n=== 页号单调性 ===")
drops = [(i, toc[i - 1][2], toc[i][2]) for i in range(1, len(toc)) if toc[i][2] < toc[i - 1][2]]
print(f"  页号回退 {len(drops)} 处")
for i, a, b in drops[:10]:
    print(f"    #{i}: p{a} -> p{b}   {toc[i][1][:44]!r}")

# 3) 真正调用 set_toc，暴露异常
print("\n=== set_toc 实测 ===")
doc = pymupdf.open("origin/BMS-Training-Manual_v2_demo.pdf")
try:
    doc.set_toc(toc)
    print(f"  set_toc OK -> {len(doc.get_toc())} 条")
except Exception as e:
    print(f"  set_toc 抛异常: {type(e).__name__}: {e}")
    # 逐步缩短找最小失败前缀
    for n in (1, 5, 20, 50, 100, len(toc)):
        d2 = pymupdf.open("origin/BMS-Training-Manual_v2_demo.pdf")
        try:
            d2.set_toc(toc[:n])
            print(f"    前 {n} 条: OK")
        except Exception as e2:
            print(f"    前 {n} 条: FAIL {type(e2).__name__}: {str(e2)[:120]}")
        d2.close()
doc.close()

# 4) 归一化 level 后再试
print("\n=== 归一化 level 后重试 ===")
norm = []
prev = 0
for lv, t, pg in toc:
    lv = int(lv)
    if prev == 0:
        lv = 1
    elif lv > prev + 1:
        lv = prev + 1
    norm.append([lv, t, int(pg)])
    prev = lv
d3 = pymupdf.open("origin/BMS-Training-Manual_v2_demo.pdf")
try:
    d3.set_toc(norm)
    print(f"  归一化后 set_toc OK -> {len(d3.get_toc())} 条")
except Exception as e:
    print(f"  归一化后仍失败: {type(e).__name__}: {e}")
d3.close()
