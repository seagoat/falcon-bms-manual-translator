"""Lead: inspect the exact line texts the /translated-layout API gives the front-end,
and compare with compute_layout's output, to find where page numbers vanish."""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from core import db as cdb, store  # noqa: E402
from render import pagebuild as pb  # noqa: E402

print("=== A) 服务端 /translated-layout 的 p4 点线行 ===")
with urllib.request.urlopen(
        "http://127.0.0.1:8777/api/documents/1/versions/2/pages/4/translated-layout",
        timeout=90) as r:
    lay = json.load(r)
print(f"  boxes={len(lay.get('boxes', []))}  font={lay.get('font_file')}")
n = 0
for b in lay.get("boxes", []):
    for ln in b.get("lines", []):
        t = ln.get("text", "")
        if "...." in t:
            print(f"    seg={b.get('seg_id')} x={ln['x']:.1f} w={ln['width']:.1f} "
                  f"末端={ln['x']+ln['width']:.1f}")
            print(f"        {t[-40:]!r}")
            n += 1
            break
    if n >= 6:
        break

print("\n=== B) compute_layout 同一页（对照）===")
con = cdb.connect()
segs = store.segments_of_version(con, 2, page=4)
trs = store.translations_of_version(con, 2)
con.close()
boxes = pb.compute_layout(segs, trs, page_no=4)
n = 0
for b in boxes:
    for ln in b["lines"]:
        if "...." in ln["text"]:
            print(f"    seg={b['seg_id']} x={ln['x']:.1f} w={ln['width']:.1f} "
                  f"末端={ln['x']+ln['width']:.1f}")
            print(f"        {ln['text'][-40:]!r}")
            n += 1
            break
    if n >= 6:
        break
