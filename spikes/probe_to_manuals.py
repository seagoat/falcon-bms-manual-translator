"""Lead: read-only viability probe for the two TO manuals (objective on-ramp).

Does NOT ingest into the frozen DB. Checks whether the existing extraction/rendering
pipeline can handle their page geometry, fonts and text layer.
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

FILES = [
    "origin/TO 1F-16CMAM-1 BMS.pdf",
    "origin/TO 1F-16CMAM-34-1-1 BMS.pdf",
]

for path in FILES:
    if not os.path.exists(path):
        print(f"MISSING {path}")
        continue
    d = pymupdf.open(path)
    print(f"\n{'=' * 78}\n{os.path.basename(path)}")
    print(f"  pages={d.page_count}  size={os.path.getsize(path)/1e6:.1f} MB  "
          f"toc={len(d.get_toc())}")
    md = {k: v for k, v in (d.metadata or {}).items() if v}
    print(f"  title={md.get('title')!r} author={md.get('author')!r}")
    print(f"  producer={str(md.get('producer'))[:60]!r}")

    # page geometry spread
    sizes = Counter()
    imgs = 0
    chars = 0
    fonts = Counter()
    for i in range(d.page_count):
        p = d[i]
        sizes[(round(p.rect.width), round(p.rect.height))] += 1
        imgs += len(p.get_images(full=True))
        t = p.get_text()
        chars += len(t)
        for b in p.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for s in ln["spans"]:
                    fonts[(s["font"], round(s["size"], 1))] += 1
    print(f"  total text chars={chars}  images={imgs}")
    print(f"  page sizes (pt): {dict(sizes.most_common(4))}")
    print(f"  top fonts:")
    for k, n in fonts.most_common(6):
        print(f"    {n:6d}  {k}")

    # does page 20 look like a normal text page?
    probe = min(20, d.page_count - 1)
    pg = d[probe]
    txt = pg.get_text()
    print(f"  page {probe+1}: text_chars={len(txt)} images={len(pg.get_images(full=True))} "
          f"drawings={len(pg.get_drawings())}")
    print(f"    sample: {txt[:200]!r}")
    d.close()
