"""诊断：/api/.../pages/N/image?kind=cn 的译文页重建是否真的生效。

用法：python web/static/_devtest/probe_cn_render.py [page] [version_id]
"""
from __future__ import annotations

import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402
from core import db as core_db  # noqa: E402
from core import store as core_store  # noqa: E402
import render.pagebuild as pb  # noqa: E402

PAGE = int(sys.argv[1]) if len(sys.argv) > 1 else 21
VID = int(sys.argv[2]) if len(sys.argv) > 2 else 2


def main() -> int:
    with closing(core_db.connect()) as c:
        v = core_store.get_version(c, VID)
        segs = core_store.segments_of_version(c, VID, page=PAGE)
        trans = core_store.translations_of_version(c, VID)
    print("source_path:", v.get("source_path"), "exists:", Path(v.get("source_path", "")).exists())
    body = [s for s in segs if s.get("role") == "body"]
    with_tr = [s for s in body if trans.get(s["id"])]
    print(f"page {PAGE}: segments={len(segs)} body={len(body)} 有译文={len(with_tr)}")
    for s in body[:4]:
        t = trans.get(s["id"]) or {}
        print(f"   seg {s['id']} bbox={[round(x,1) for x in (s.get('bbox') or [])]} "
              f"status={t.get('status')} zh={str(t.get('text'))[:28]!r}")
    if not with_tr:
        print("!! 该页没有任何带译文的 body 段 → 重建自然只会擦除 0 个 bbox")

    stats: dict = {}
    src = pymupdf.open(v["source_path"])
    try:
        page = pb.build_translated_page(src, PAGE, segs, trans, cjk_font_path=pb.resolve_cjk_font(),
                                        stats=stats)
        txt = page.get_text()
        cjk = [ch for ch in txt if "\u4e00" <= ch <= "\u9fff"]
        print("build_translated_page stats:", stats)
        print("结果页中文数:", len(cjk), "| 前 120 字:", repr(txt[:120]))
        ok = len(cjk) > 0
        print("==>", "PASS 译文页确实被重建（含中文）" if ok else "FAIL 重建后仍无中文（等于原页）")
        return 0 if ok else 1
    finally:
        src.close()


if __name__ == "__main__":
    sys.exit(main())
