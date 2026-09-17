"""Lead: verify the CN outline is generated and embedded in an exported PDF."""
import os
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf

from core import db as cdb, store
from render import pdfout

con = cdb.connect()
v = store.get_version(con, 2)
src = pdfout._resolve_source(con, 2) if hasattr(pdfout, "_resolve_source") else None
src = src or "origin/BMS-Training-Manual_v2_demo.pdf"
prev = "origin/BMS-Training-Manual.pdf"

toc = pdfout.build_cn_outline(con, 2, prev_pdf=prev)
print(f"生成的 CN 书签: {len(toc)} 条")
print("层级分布:")
from collections import Counter  # noqa: E402
print(" ", dict(sorted(Counter(t[0] for t in toc).items())))
print("\n前 12 条:")
for lv, t, pg in toc[:12]:
    print(f"  {'  ' * (lv - 1)}L{lv} p{pg:3d}  {t[:56]}")

# 导出 3 页验证书签真的嵌进去了
segs = store.segments_of_version(con, 2)
trs = store.translations_of_version(con, 2)
res = pdfout.export_cn_pdf(src, "data/derived/_toc_verify.pdf", segs, trs,
                           pages=list(range(1, 32)), cn_toc=toc)
print(f"\n导出: pages={res['pages']} bytes={res['bytes']}")

d = pymupdf.open("data/derived/_toc_verify.pdf")
t = d.get_toc()
print(f"导出 PDF 的书签数: {len(t)}")
inrange = [x for x in t if x[2] <= 31]
print(f"  其中页号 <=31 的: {len(inrange)}")
for x in inrange[:12]:
    print(f"    L{x[0]} p{x[2]:3d}  {x[1][:56]}")
d.close()
con.close()
os.remove("data/derived/_toc_verify.pdf")
print("\n（已清理临时导出）")
