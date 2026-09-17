"""Lead: verify the page-image pipeline serves the correct page.

Compares the cached page image for page N against a direct render of PDF page N,
for several N, to find any off-by-k in the image cache/manifest.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import urllib.request  # noqa: E402

import pymupdf  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
OUT = "data/derived/preview"
PDF = "origin/BMS-Training-Manual_v2_demo.pdf"

doc = pymupdf.open(PDF)


def sha(b):
    return hashlib.sha256(b).hexdigest()[:16]


print("=== what text does each PDF page contain (footer/lead) ===")
for n in (1, 2, 3, 21, 22, 31):
    t = doc[n - 1].get_text().replace("\n", " ")
    t = " ".join(t.split())
    print(f"  pdf page {n:3d}: {t[:110]!r}")

print("\n=== served image vs direct render ===")
for n in (1, 2, 21, 22):
    url = f"{BASE}/api/documents/1/versions/2/pages/{n}/image?kind=source&dpi=110"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            served = r.read()
    except Exception as e:
        print(f"  page {n}: HTTP ERR {e}")
        continue
    direct = doc[n - 1].get_pixmap(dpi=110).tobytes("png")
    print(f"  page {n:3d}: served {len(served):7d} B sha={sha(served)}   "
          f"direct-render {len(direct):7d} B sha={sha(direct)}")

    # save served image and OCR-lite: check for the printed page number in image space
    ext = ".webp" if served[:4] == b"RIFF" else ".png"
    p = os.path.join(OUT, f"_svc_p{n}{ext}")
    open(p, "wb").write(served)

print("\n=== markers: seg count + page field per requested page ===")
import json  # noqa: E402
for n in (1, 2, 21):
    url = f"{BASE}/api/documents/1/versions/2/pages/{n}/markers"
    with urllib.request.urlopen(url, timeout=60) as r:
        m = json.load(r)
    items = m.get("items", [])
    segs = [it.get("seg_id") for it in items][:3]
    print(f"  requested page {n:3d}: {len(items):3d} markers, first segs={segs}")

print("\n=== DB: which segments belong to page 1 vs 21 ===")
from core import db as cdb  # noqa: E402
con = cdb.connect()
for n in (1, 2, 21):
    rows = con.execute("SELECT id, order_index, role, substr(text,1,40) t FROM segments "
                       "WHERE version_id=2 AND page=? ORDER BY order_index LIMIT 4", (n,)).fetchall()
    print(f"  db page {n:3d}:")
    for r in rows:
        print(f"     seg={r['id']} ord={r['order_index']} {r['role']:7s} {r['t']!r}")
con.close()
doc.close()
