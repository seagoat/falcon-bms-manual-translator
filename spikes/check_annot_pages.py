"""Lead: check which source pages made it into annotated_full.pdf."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

SRC = "origin/BMS-Training-Manual_v2_demo.pdf"
ANN = "data/derived/annotated_full.pdf"

ann = pymupdf.open(ANN)
print(f"annotated_full.pdf: {ann.page_count} 页")
print("\n逐页比对（用页脚页码 / 页眉识别原始页号）:")
for i in range(ann.page_count):
    pg = ann[i]
    # printed page number is bottom-right, Calibri-Bold 12pt, white on banner
    num = None
    for b in pg.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b.get("lines", []):
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if txt.isdigit() and ln["bbox"][1] > 770:
                num = int(txt)
    t = pg.get_text()
    cjk = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
    print(f"  out#{i+1:2d}  原书页={num}  中文字符={cjk}")

ann.close()

# which source pages have labels
ocr = "data/derived/ocr"
pages = sorted({int(f[1:].split("_")[0]) for f in os.listdir(ocr) if f.endswith("_zh.json")})
print(f"\n有标签数据的源页: {pages}")
