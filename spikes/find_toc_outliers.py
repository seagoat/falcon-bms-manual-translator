"""Lead: identify the dot lines in the TOCs that do NOT end with a page number."""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf

for path, page, label in (("data/derived/full_cn.pdf", 4, "训练手册CN"),
                          ("data/derived/to34_full_cn.pdf", 5, "TO-34")):
    d = pymupdf.open(path)
    print(f"\n=== {label}  {path} p{page} 不以数字结尾的点线行 ===")
    for b in d[page - 1].get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b.get("lines", []):
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if t and "..." in t and not t[-1].isdigit():
                bb = ln["bbox"]
                print(f"   x {bb[0]:6.1f}-{bb[2]:6.1f}  {t[-60:]!r}")
    d.close()
