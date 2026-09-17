"""Lead: find the TOC page numbers in the ORIGINAL pdf and in our segments."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

from core import db as cdb, store  # noqa: E402

ORIG = "origin/BMS-Training-Manual_v2_demo.pdf"
d = pymupdf.open(ORIG)
p = d[3]
print(f"=== 原文 p4 raw text（前 900 字）===")
print(repr(p.get_text()[:900]))

print("\n=== 原文 p4 里所有 x>520 的 span ===")
for b in p.get_text("dict")["blocks"]:
    if b["type"] != 0:
        continue
    for ln in b.get("lines", []):
        for s in ln["spans"]:
            if s["bbox"][0] > 520 and s["text"].strip():
                print(f"  x {s['bbox'][0]:6.1f}-{s['bbox'][2]:6.1f} "
                      f"y {s['bbox'][1]:6.1f}  {s['text']!r}")
d.close()

print("\n=== 我们库里的 p4 段：文本以数字结尾的 ===")
con = cdb.connect()
for s in store.segments_of_version(con, 2, page=4):
    t = (s["text"] or "").strip()
    if re.search(r"\d\s*$", t) and "...." in t:
        print(f"  seg={s['id']} x1={s['bbox'][2]:6.1f} {t[-40:]!r}")
print("（若上面为空，说明抽取阶段把页码丢掉了）")

# 该页原始行数 vs 抽取段数
d = pymupdf.open(ORIG)
lines = []
for b in d[3].get_text("dict")["blocks"]:
    for ln in b.get("lines", []):
        if "".join(x["text"] for x in ln["spans"]).strip():
            lines.append(ln)
print(f"\n原文 p4 有文字的行数: {len(lines)}")
print(f"我们抽出的 p4 段数: {len(store.segments_of_version(con, 2, page=4))}")
d.close()
con.close()
