"""Lead: final check — TOC page numbers present in all four PDFs."""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf

FILES = [
    ("data/derived/full_cn.pdf", 4, "训练手册CN"),
    ("data/derived/full_bi.pdf", 4, "训练手册双语"),
    ("data/derived/to1_full_cn.pdf", 3, "TO-1"),
    ("data/derived/to34_full_cn.pdf", 5, "TO-34"),
]
for path, page, label in FILES:
    d = pymupdf.open(path)
    toc = d.get_toc()
    # 统计该页以数字结尾的行数（目录页码列）
    n_num = 0
    n_dots = 0
    for b in d[page - 1].get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b.get("lines", []):
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if not t:
                continue
            if "..." in t:
                n_dots += 1
                if t[-1].isdigit():
                    n_num += 1
    print(f"{label:16s} {path}")
    print(f"   pages={d.page_count} 书签={len(toc)} 目录页 p{page}: "
          f"点线行={n_dots} 其中以页码结尾={n_num}")
    d.close()
