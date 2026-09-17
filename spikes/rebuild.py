"""Spike B: element-level page rebuild, then CJK redraw on top.

Instead of rasterizing the original page, rebuild it from primitives:
  images (insert_image with transform) + vector drawings (Shape) + text (TextWriter)
This keeps an editable text layer and avoids flattening.
"""
import os

import pymupdf

PDF = r"E:\worksrc\manual_trans_trace\origin\BMS-Training-Manual.pdf"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
FONT = r"C:\Windows\Fonts\Deng.ttf"
FONT_L = r"C:\Windows\Fonts\calibri.ttf"
DPI = 110


def image_rects(page):
    """Map image xref -> placement rect+transform via get_image_info."""
    out = []
    for info in page.get_image_info(xrefs=True):
        out.append({
            "xref": info["xref"],
            "bbox": pymupdf.Rect(info["bbox"]),
            "width": info["width"],
            "height": info["height"],
            "transform": info.get("transform"),
        })
    return out


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    doc = pymupdf.open(PDF)
    pno = 20
    src = doc[pno]

    out = pymupdf.open()
    ps = out.new_page(width=src.rect.width, height=src.rect.height)

    infos = image_rects(src)
    print(f"{len(infos)} image placements")
    for it in infos:
        r = it["bbox"]
        print(f"  xref={it['xref']} bbox={[round(v,1) for v in r]} px={it['width']}x{it['height']} "
              f"ratio=({r.width/it['width']:.4f},{r.height/it['height']:.4f})")
        try:
            pix = pymupdf.Pixmap(doc, it["xref"])
            if pix.colorspace is None or pix.n - pix.alpha not in (1, 3):
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            ps.insert_image(r, pixmap=pix)
        except Exception as e:
            print("   ! insert failed:", e)

    # drawings
    dr = src.get_drawings()
    print(f"{len(dr)} drawings")
    shape = ps.new_shape()
    for d in dr:
        for item in d["items"]:
            k = item[0]
            if k == "l":
                shape.draw_line(item[1], item[2])
            elif k == "re":
                shape.draw_rect(item[1])
            elif k == "c":
                shape.draw_bezier(item[1], item[2], item[3], item[4])
            elif k == "qu":
                shape.draw_quad(item[1])
        shape.finish(color=d.get("color"), fill=d.get("fill"), width=d.get("width", 0),
                     closePath=d.get("closePath", False), even_odd=d.get("even_odd", False))
    shape.commit()

    # original text, mixed fonts
    tw = pymupdf.TextWriter(ps.rect)
    f_lat = pymupdf.Font(fontfile=FONT_L)
    for b in src.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            for s in line["spans"]:
                if not s["text"]:
                    continue
                tw.append(pymupdf.Point(s["origin"][0], s["origin"][1]), s["text"],
                          font=f_lat, fontsize=s["size"])
    tw.write_text(ps)
    ps.get_pixmap(dpi=DPI).save(os.path.join(OUT, f"p{pno+1}_rebuild.png"))
    out.save(os.path.join(OUT, f"p{pno+1}_rebuild.pdf"))

    # difference metric vs original render
    a = pymupdf.Pixmap(os.path.join(OUT, f"p{pno+1}_base.png"))
    b2 = pymupdf.Pixmap(os.path.join(OUT, f"p{pno+1}_rebuild.png"))
    print("base", a.width, a.height, "rebuild", b2.width, b2.height)
    doc.close()
    out.close()


if __name__ == "__main__":
    main()
