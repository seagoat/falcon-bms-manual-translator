"""Spike: inspect page geometry, colors, drawings of the BMS manual."""
import json
import pymupdf

PDF = r"E:\worksrc\manual_trans_trace\origin\BMS-Training-Manual.pdf"


def main() -> None:
    d = pymupdf.open(PDF)
    for pno in (14, 20, 100, 300):
        p = d[pno]
        print(f"===== page {pno + 1}  cropbox={p.cropbox} rot={p.rotation}")
        for b in p.get_text("dict")["blocks"]:
            if b["type"] == 1:
                print(f"  IMG bbox={[round(x, 1) for x in b['bbox']]} w={b.get('width')} h={b.get('height')}")
                continue
            for line in b.get("lines", []):
                for s in line["spans"]:
                    txt = s["text"]
                    print(
                        f"  span sz={s['size']:.1f} f={s['font']:18s} "
                        f"c=#{s['color']:06x} bbox={[round(x, 1) for x in s['bbox']]} flags={s['flags']} "
                        f"{txt[:70]!r}"
                    )
        dr = p.get_drawings()
        if dr:
            print(f"  --- {len(dr)} drawings")
            for x in dr[:5]:
                print(
                    f"    fill={x.get('fill')} color={x.get('color')} w={x.get('width')} "
                    f"rect={[round(v, 1) for v in x['rect']]} items={len(x['items'])}"
                )
        print()


if __name__ == "__main__":
    main()
