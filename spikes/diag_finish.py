"""Lead: test pdfout._finish in isolation to find the TOC loss."""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf

from core import db as cdb
from render import pdfout

con = cdb.connect()
toc = pdfout.build_cn_outline(con, 2, prev_pdf="origin/BMS-Training-Manual.pdf")
con.close()
print(f"toc: {len(toc)} 条")

src = pymupdf.open("origin/BMS-Training-Manual_v2_demo.pdf")
final = pymupdf.open()
for i in range(1, 6):
    final.new_page(width=595, height=842)
print("合成 final 页数:", final.page_count)

out = "data/derived/_finish_test.pdf"
try:
    n = pdfout._finish(final, out, src, True, " (测试)", cn_toc=toc)
    print("_finish 返回:", n)
except Exception as e:
    print("_finish 抛异常:", type(e).__name__, e)
finally:
    try:
        final.close()
    except Exception:
        pass
    src.close()

d = pymupdf.open(out)
print("落盘后书签数:", len(d.get_toc()))
d.close()

# 用最强参数再试一次
src2 = pymupdf.open("origin/BMS-Training-Manual_v2_demo.pdf")
f2 = pymupdf.open()
for i in range(1, 6):
    f2.new_page(width=595, height=842)
f2.set_toc(toc)
print("直接 set_toc 后内存中:", len(f2.get_toc()))
f2.save("data/derived/_finish_test2.pdf", garbage=3, deflate=True)
f2.close()
d2 = pymupdf.open("data/derived/_finish_test2.pdf")
print("save 后重新打开:", len(d2.get_toc()))
d2.close()
src2.close()
