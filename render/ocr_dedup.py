"""Lead: dedup OCR boxes against the main pipeline's segments.

OCR picks up body text that merely happens to sit inside an image's bounding box
(e.g. p25's checklist table). Those are already translated by the main pipeline, so
annotating them twice is wrong. Keep only OCR boxes that are NOT covered by a
segment already handled by the text pipeline.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb  # noqa: E402


def norm(s: str) -> str:
    import re
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9]+", "", s)
    return s


def overlap_frac(a, b) -> float:
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix <= 0 or iy <= 0:
        return 0.0
    inter = ix * iy
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return inter / area


def dedup(page_no: int, boxes: list, segs: list, *, verbose=False) -> tuple:
    """boxes: OCR boxes in PAGE pt. segs: DB segments (bbox + text) for that page."""
    seg_boxes = []
    for s in segs:
        bb = s["bbox"]
        if not bb or len(bb) < 4:
            continue
        seg_boxes.append(([bb[0], bb[1], bb[2], bb[3]], norm(s["text"])))

    kept, dropped_text, dropped_geo = [], 0, 0
    for b in boxes:
        lab = norm(b.get("text") or b.get("label") or "")
        # 1) text-identity match: OCR box text ≈ an existing segment's text
        hit_text = False
        for sb, st in seg_boxes:
            if not st or not lab:
                continue
            if st == lab or (len(lab) >= 4 and (lab in st or st in lab)):
                hit_text = True
                break
        if hit_text:
            dropped_text += 1
            if verbose:
                print(f"    drop(text)  {b.get('text')!r}")
            continue
        # 2) geometry: OCR box almost entirely inside a segment box whose text is
        #    long (body paragraph) -> it is body text captured by a big image bbox
        hit_geo = False
        for sb, st in seg_boxes:
            if len(st) < 25:
                continue
            if overlap_frac([b["x0"], b["y0"], b["x1"], b["y1"]], sb) > 0.75:
                hit_geo = True
                break
        if hit_geo:
            dropped_geo += 1
            if verbose:
                print(f"    drop(geo)   {b.get('text')!r}")
            continue
        kept.append(b)
    return kept, dropped_text, dropped_geo


def main() -> int:
    import json
    con = cdb.connect()
    for pno in [int(x) for x in sys.argv[1:]] or [25, 141, 11, 114, 291]:
        p = f"data/derived/ocr/p{pno}_ocr.json"
        if not os.path.exists(p):
            print(f"p{pno}: no OCR json")
            continue
        data = json.load(open(p, encoding="utf-8"))
        boxes = data["texts"]
        segs = con.execute("SELECT bbox, text FROM segments WHERE version_id=2 AND page=?",
                           (pno,)).fetchall()
        import json as J
        segs = [{"bbox": J.loads(r["bbox"] or "[]"), "text": r["text"] or ""} for r in segs]
        kept, dt, dg = dedup(pno, boxes, segs, verbose=(len(sys.argv) > 1))
        print(f"p{pno:3d}: ocr={len(boxes):3d} segs={len(segs):3d} "
              f"-> kept={len(kept):3d} (drop text={dt} geo={dg})")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
