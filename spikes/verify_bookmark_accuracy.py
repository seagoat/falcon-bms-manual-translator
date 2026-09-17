"""Lead: sanity-check that bookmarks point at pages containing their title."""
import random
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf

for path, label in (("data/derived/full_cn.pdf", "训练手册"),
                    ("data/derived/to1_full_cn.pdf", "TO-1"),
                    ("data/derived/to34_full_cn.pdf", "TO-34")):
    d = pymupdf.open(path)
    toc = d.get_toc()
    if not toc:
        print(f"{label}: 无书签")
        d.close()
        continue
    random.seed(11)
    sample = random.sample(toc, min(12, len(toc)))
    hit = 0
    print(f"\n=== {label}  共 {len(toc)} 条书签 ===")
    for lv, t, pg in sorted(sample, key=lambda x: x[2]):
        txt = " ".join(d[pg - 1].get_text().split())
        key = "".join(ch for ch in t[:10] if ch.isalnum())
        ok = bool(key) and key in "".join(ch for ch in txt if ch.isalnum())
        hit += 1 if ok else 0
        print(f"  {'OK ' if ok else '?? '} L{lv} p{pg:4d}  {t[:46]}")
    print(f"  → {hit}/{len(sample)} 条书签在其目标页上找得到标题")
    d.close()
