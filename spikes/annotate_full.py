"""Lead: render the full annotated PDF for all pages that have label data."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

from render import annotate  # noqa: E402

SRC = "origin/BMS-Training-Manual_v2_demo.pdf"
OUT_PDF = "data/derived/annotated_full.pdf"
OCR_DIR = "data/derived/ocr"

pages = []
for f in sorted(os.listdir(OCR_DIR)):
    if f.endswith("_zh.json"):
        pages.append(int(f[1:].split("_")[0]))
pages = sorted(set(pages))
print(f"有标注数据的页: {len(pages)}  {pages}")

src = pymupdf.open(SRC)
out = pymupdf.open()
stats = []
for idx, pno in enumerate(pages):
    st = annotate.annotate_page(src, pno, out, verbose=False)
    stats.append((pno, st["planned"], st["drawn"]))
    print(f"  p{pno:3d}: 计划 {st['planned']:3d} -> 绘制 {st['drawn']:3d}")
out.save(OUT_PDF)
print(f"\n导出 {OUT_PDF}  共 {len(pages)} 页")
total_planned = sum(s[1] for s in stats)
total_drawn = sum(s[2] for s in stats)
print(f"标签合计: 计划 {total_planned} / 绘制 {total_drawn} "
      f"（{total_drawn/max(1,total_planned)*100:.0f}%）")

# 抽样渲染对比图
DPI = 120
for pno in (11, 114, 302, 356):
    if pno not in pages:
        continue
    i = pages.index(pno)
    a = src[pno - 1].get_pixmap(dpi=DPI)
    b = out[i].get_pixmap(dpi=DPI)
    from PIL import Image
    ia = Image.frombytes("RGB", (a.width, a.height), a.samples)
    ib = Image.frombytes("RGB", (b.width, b.height), b.samples)
    gap = 16
    canvas = Image.new("RGB", (a.width + gap + b.width, max(a.height, b.height)), (40, 40, 40))
    canvas.paste(ia, (0, 0))
    canvas.paste(ib, (a.width + gap, 0))
    canvas.save(f"data/derived/preview/annotfull_cmp_p{pno}.png")
    out[i].get_pixmap(dpi=DPI).save(f"data/derived/preview/annotfull_p{pno}.png")
print("已保存抽样对比图 annotfull_cmp_p{11,114,302,356}.png")

json.dump([{"page": p, "planned": a, "drawn": b} for p, a, b in stats],
          open("data/derived/ocr/annotate_full_report.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
src.close()
out.close()
