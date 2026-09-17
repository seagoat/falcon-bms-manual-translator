"""Spike: verify redact + CJK redraw preserves layout (images, banners, colors).

Produces spikes/out/ samples for visual inspection:
  - p15_base.png      original render
  - p15_redact.png    after erasing body text (images + banners must survive)
  - p15_cn.png        after drawing CJK paragraphs with wrapped lines
"""
import os

import pymupdf

PDF = r"E:\worksrc\manual_trans_trace\origin\BMS-Training-Manual.pdf"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
FONT = r"C:\Windows\Fonts\Deng.ttf"
FONT_B = r"C:\Windows\Fonts\Dengb.ttf"
DPI = 110

# Hand-written sample translations keyed by the English source text.
SAMPLE = {
    "1.4.1 MASTER CAUTION light ": "1.4.1 主警戒灯（MASTER CAUTION light）",
    "The MASTER CAUTION light activates shortly after any individual light on the Cau-": None,  # paragraph handled below
    "CANOPY ": "座舱盖（CANOPY）",
    "FLCS ": "飞控系统（FLCS）",
    "HYD/OIL PRESS ": "液压/滑油压力（HYD/OIL PRESS）",
}


def full_page_images(doc) -> set:
    """Find image xrefs that repeat on (nearly) every page -> banners."""
    from collections import Counter

    c = Counter()
    for p in doc:
        for im in p.get_images(full=True):
            c[im[0]] += 1
    return {x for x, n in c.items() if n > doc.page_count * 0.8}


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    doc = pymupdf.open(PDF)
    banners = full_page_images(doc)
    print("banner xrefs:", banners)

    pno = 20  # page 21 - dense text + small images
    src = doc[pno]
    print("page rect:", src.rect)

    # 1. base render
    src.get_pixmap(dpi=DPI).save(os.path.join(OUT, f"p{pno+1}_base.png"))

    # 2. rebuild page as a single-page doc, then erase body text
    out = pymupdf.open()
    stage = pymupdf.open()
    np = stage.new_page(width=src.rect.width, height=src.rect.height)
    np.show_pdf_page(np.rect, doc, pno)

    # collect body text rects to erase
    body_rects = []
    d = src.get_text("dict")
    for b in d["blocks"]:
        if b["type"] == 1 and b.get("number", 0) in _image_numbers(src, banners):
            continue
        for line in b.get("lines", []):
            for s in line["spans"]:
                if s["text"].strip():
                    body_rects.append(pymupdf.Rect(s["bbox"]))
    union = pymupdf.Rect()
    for r in body_rects:
        union |= r
    print("body union:", union, "spans:", len(body_rects))

    for r in body_rects:
        np.add_redact_annot(r)
    np.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    np.get_pixmap(dpi=DPI).save(os.path.join(OUT, f"p{pno+1}_redact.png"))

    # 3. draw CJK paragraphs. Simple greedy wrapper over the paragraph bbox.
    font = pymupdf.Font(fontfile=FONT)
    fontb = pymupdf.Font(fontfile=FONT_B)
    ps = out.new_page(width=src.rect.width, height=src.rect.height)
    ps.show_pdf_page(ps.rect, stage, 0)
    tw = pymupdf.TextWriter(ps.rect)

    # group spans into paragraphs by (block, line-group)
    paras = []
    for b in d["blocks"]:
        if b["type"] != 0:
            continue
        cur = None
        for line in b.get("lines", []):
            for s in line["spans"]:
                if not s["text"].strip():
                    continue
                if cur is None or abs(s["size"] - cur["size"]) > 0.6:
                    cur = {"size": s["size"], "bold": "Bold" in s["font"], "color": s["color"],
                           "bbox": pymupdf.Rect(s["bbox"]), "text": ""}
                    paras.append(cur)
                cur["bbox"] |= pymupdf.Rect(s["bbox"])
                cur["text"] += s["text"]
    # merge consecutive paras that share the same line rhythm (rough)
    print(f"{len(paras)} raw paragraph candidates")

    n_drawn = 0
    for p in paras:
        key = p["text"].strip()
        cn = SAMPLE.get(key + " ")
        if cn is None:
            cn = None
        if not cn:
            continue
        r = p["bbox"]
        f = fontb if p["bold"] else font
        fs = p["size"] * 1.02
        lines = wrap_cjk(cn, f, fs, r.width)
        line_h = fs * 1.32
        y = r.y0 + fs * 0.96
        for ln in lines:
            tw.append(pymupdf.Point(r.x0, y), ln, font=f, fontsize=fs)
            y += line_h
        n_drawn += 1
    tw.write_text(ps)
    ps.get_pixmap(dpi=DPI).save(os.path.join(OUT, f"p{pno+1}_cn.png"))
    print("drew", n_drawn, "paragraphs")
    out.close()
    doc.close()


def _image_numbers(page, banners):
    return {
        i for i, im in enumerate(page.get_images(full=True)) if im[0] in banners
    }


def wrap_cjk(text, font, size, maxw):
    lines, cur = [], ""
    for ch in text:
        t = cur + ch
        if font.text_length(t, size) > maxw and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


if __name__ == "__main__":
    main()
