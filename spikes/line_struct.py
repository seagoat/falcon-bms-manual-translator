"""Spike: examine the exact line structures around hyphenation boundaries."""
import pymupdf

PDF = r"E:\worksrc\manual_trans_trace\origin\BMS-Training-Manual.pdf"


def main() -> None:
    doc = pymupdf.open(PDF)
    for pno in (14, 21, 22, 23):
        p = doc[pno]
        print(f"===== page {pno+1}")
        for bi, b in enumerate(p.get_text("dict")["blocks"]):
            if b["type"] != 0:
                continue
            for li, line in enumerate(b.get("lines", [])):
                txt = "".join(s["text"] for s in line["spans"])
                if not txt.strip():
                    continue
                bb = line["bbox"]
                print(f"  blk{bi} ln{li} x0={bb[0]:6.1f} x1={bb[2]:6.1f} y0={bb[1]:6.1f} "
                      f"sz={line['spans'][0]['size']:.1f} {txt!r}")
            print("  ---")
        print()


if __name__ == "__main__":
    main()
