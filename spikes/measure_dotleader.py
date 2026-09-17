"""Lead: check TOC dot-leader alignment - do translated lines end at the same x
as the original (so page numbers line up in a column)?"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402

CN = "data/derived/full_cn.pdf"
ORIG = "origin/BMS-Training-Manual_v2_demo.pdf"
PAGES = [4, 5]

cn = pymupdf.open(CN)
orig = pymupdf.open(ORIG)


def lines(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for ln in b.get("lines", []):
            t = "".join(s["text"] for s in ln["spans"]).strip()
            if t:
                out.append((ln["bbox"], t))
    out.sort(key=lambda x: (round(x[0][1], 0), x[0][0]))
    return out


for pno in PAGES:
    ol, cl = lines(orig[pno - 1]), lines(cn[pno - 1])
    print(f"\n{'=' * 96}\npage {pno}: original {len(ol)} lines, cn {len(cl)} lines")
    print(f"{'ORIG right':>10} {'CN right':>9} {'Δ':>6}  {'dots?':5} text")
    # match by vertical proximity
    used = set()
    for obb, ot in ol:
        best, bd = None, 9e9
        for i, (cbb, ct) in enumerate(cl):
            if i in used:
                continue
            d = abs(cbb[1] - obb[1])
            if d < bd:
                bd, best = d, i
        if best is None or bd > 6:
            print(f"{obb[2]:10.1f} {'--':>9} {'':>6}  {'':5} {ot[:60]!r}  (no cn match)")
            continue
        used.add(best)
        cbb, ct = cl[best]
        dot_o = "...." in ot
        dot_c = "...." in ct
        flag = "  <<<" if dot_o and abs(cbb[2] - obb[2]) > 3 else ""
        print(f"{obb[2]:10.1f} {cbb[2]:9.1f} {cbb[2]-obb[2]:6.1f}  "
              f"{'Y' if dot_o else 'n'}/{'Y' if dot_c else 'n'}   {ct[:52]!r}{flag}")

    # how many dot-leader lines are right-edge aligned within 2pt?
    pairs = []
    used = set()
    for obb, ot in ol:
        if "...." not in ot:
            continue
        best, bd = None, 9e9
        for i, (cbb, ct) in enumerate(cl):
            if i in used:
                continue
            d = abs(cbb[1] - obb[1])
            if d < bd:
                bd, best = d, i
        if best is not None and bd <= 6:
            used.add(best)
            pairs.append(abs(cl[best][0][2] - obb[2]))
    if pairs:
        import statistics
        print(f"  dot-leader lines: {len(pairs)}, right-edge |Δ| "
              f"median={statistics.median(pairs):.2f}pt  max={max(pairs):.2f}pt  "
              f"within2pt={sum(1 for d in pairs if d <= 2)}/{len(pairs)}")

cn.close()
orig.close()
