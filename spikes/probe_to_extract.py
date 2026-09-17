"""Lead: read-only segmentation probe on the TO manuals (no DB writes).

Runs pipeline.extract_segments page by page and reports whether the extractor
handles the different page geometry (US Letter, 5 landscape pages) and fonts.
"""
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402
import pipeline  # noqa: E402

FILES = [
    ("TO 1F-16CMAM-1 BMS.pdf", 404),
    ("TO 1F-16CMAM-34-1-1 BMS.pdf", 669),
]

for name, npages in FILES:
    path = os.path.join("origin", name)
    d = pymupdf.open(path)
    print(f"\n{'=' * 78}\n{name}  ({d.page_count} pages)")

    # sample pages spread across the book (extract_segments takes 1-based page_no)
    sample = [p + 1 for p in range(0, d.page_count, max(1, d.page_count // 14))][:14]
    kinds = Counter()
    roles = Counter()
    total = 0
    empty_pages = []
    t0 = time.perf_counter()
    for pno in sample:
        segs = pipeline.extract_segments(d, pno)
        total += len(segs)
        if not segs:
            empty_pages.append(pno)
        for s in segs:
            kinds[s.kind] += 1
            roles[s.role] += 1
    dt = time.perf_counter() - t0
    per_page = total / max(1, len(sample))
    print(f"  sampled {len(sample)} pages -> {total} segs "
          f"({per_page:.1f}/page) in {dt:.2f}s  ->  est. full book "
          f"{per_page * d.page_count:.0f} segs / {dt / len(sample) * d.page_count:.0f}s")
    print(f"  kinds: {dict(kinds.most_common(8))}")
    print(f"  roles: {dict(roles)}")
    print(f"  pages with 0 segments: {empty_pages or 'none'}")

    # landscape pages must still work
    land = [i for i in range(d.page_count) if d[i].rect.width > d[i].rect.height]
    print(f"  landscape pages: {land[:8]}")
    for pno in land[:3]:
        segs = pipeline.extract_segments(d, pno + 1)
        print(f"    p{pno+1} ({d[pno].rect.width:.0f}x{d[pno].rect.height:.0f}): "
              f"{len(segs)} segs, sample={segs[0].text[:60]!r}" if segs else
              f"    p{pno+1}: 0 segs")
    d.close()
