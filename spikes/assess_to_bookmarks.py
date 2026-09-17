"""Lead: assess the native bookmark quality of the two TO manuals.

They were built with Adobe PDF Library and carry 561 / 1093 bookmarks — far richer
than the training manual's. If these map cleanly onto segments we can build a much
better Chinese outline than the v1-bookmark route used for the training manual.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

FILES = [
    ("origin/TO 1F-16CMAM-1 BMS.pdf", "TO-1", "data/_to_probe", 2),
    ("origin/TO 1F-16CMAM-34-1-1 BMS.pdf", "TO-34", "data/_to_probe", 1),
]

for path, label, db, vid in FILES:
    d = pymupdf.open(path)
    toc = d.get_toc() or []
    print(f"\n{'=' * 76}\n=== {label}  书签 {len(toc)} 条  页数 {d.page_count} ===")
    levels = {}
    for lv, t, pg in toc:
        levels[lv] = levels.get(lv, 0) + 1
    print(f"  层级分布: {dict(sorted(levels.items()))}")
    blank = sum(1 for _, t, _ in toc if not (t or "").strip())
    print(f"  空标题: {blank}")
    print(f"  前 18 条:")
    for lv, t, pg in toc[:18]:
        print(f"    {'  ' * (lv - 1)}L{lv} p{pg:4d}  {t[:66]}")
    print(f"  中间抽样:")
    step = max(1, len(toc) // 8)
    for lv, t, pg in toc[::step][:8]:
        print(f"    {'  ' * (lv - 1)}L{lv} p{pg:4d}  {t[:66]}")
    d.close()
