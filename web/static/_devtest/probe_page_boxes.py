"""对某一页，列出「有译文 / 有 layout box / 会被画成占位」的段，定位叠字来源。

用法：python web/static/_devtest/probe_page_boxes.py [base_url] [page]
"""
from __future__ import annotations

import json
import sys
import urllib.request

B = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8903").rstrip("/")
PAGE = int(sys.argv[2]) if len(sys.argv) > 2 else 21
DOC, VID = 1, 2


def api(p):
    with urllib.request.urlopen(B + p, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


segs = api(f"/api/documents/{DOC}/versions/{VID}/segments?page={PAGE}")
ly = api(f"/api/documents/{DOC}/versions/{VID}/pages/{PAGE}/translated-layout")
boxes = {b["seg_id"]: b for b in ly.get("boxes", [])}
print(f"page {PAGE}: segments={len(segs)} boxes={len(boxes)}")
print(f"{'seg_id':>8} {'kind':<11} {'role':<7} {'trans':<8} {'box':<4}  文本")
for s in segs:
    t = s.get("translation") or {}
    st = t.get("status") or "-"
    has = "YES" if s["seg_id"] in boxes else "-"
    flag = "  <<< 会被画成灰色占位" if (s.get("role") == "body" and s["seg_id"] not in boxes) else ""
    print(f"{s['seg_id']:>8} {s['kind']:<11} {s.get('role',''):<7} {st:<8} {has:<4}  {s['text'][:44]!r}{flag}")
print()
print("boxes:")
for sid, b in boxes.items():
    lines = b.get("lines", [])
    print(f"  seg={sid} lines={len(lines)} bbox={[round(x,1) for x in b.get('bbox',[])]} "
          f"first={lines[0]['text'][:30]!r}" if lines else f"  seg={sid} lines=0")
