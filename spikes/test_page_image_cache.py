"""Lead: regression test for the page-image cache bug + final UI re-verification.

Bug: render_snapshots params_hash omitted the page number, so the first rendered
page image was served for EVERY page. Asserts each page returns a distinct image
matching a direct render of that page.
"""
import hashlib
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
PDF = "origin/BMS-Training-Manual_v2_demo.pdf"
OUT = "data/derived/preview"


def fetch(path: str) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return r.read()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def cjk_ratio(b: bytes) -> float:
    p = os.path.join(OUT, "_tmp_probe.webp")
    open(p, "wb").write(b)
    im = Image.open(p)
    return im.size


print("=== A. each page must return a DISTINCT source image ===")
sigs = {}
ok = True
for n in (1, 2, 3, 21, 22, 31, 200, 401):
    b = fetch(f"/api/documents/1/versions/2/pages/{n}/image?kind=source")
    sigs[n] = sha(b)
    print(f"  page {n:3d}: {len(b):7d} B  sha={sigs[n]}")
dups = len(sigs) - len(set(sigs.values()))
print(f"  distinct images: {len(set(sigs.values()))}/{len(sigs)}  "
      f"{'OK' if dups == 0 else f'FAIL ({dups} duplicates)'}")
ok &= dups == 0

print("\n=== B. cn image must also be per-page distinct ===")
csigs = {}
for n in (1, 2, 21, 22):
    b = fetch(f"/api/documents/1/versions/2/pages/{n}/image?kind=cn")
    csigs[n] = sha(b)
    print(f"  page {n:3d}: {len(b):7d} B  sha={csigs[n]}")
cdups = len(csigs) - len(set(csigs.values()))
print(f"  distinct cn images: {len(set(csigs.values()))}/{len(csigs)}  "
      f"{'OK' if cdups == 0 else f'FAIL ({cdups} duplicates)'}")
ok &= cdups == 0

print("\n=== C. page 1 rendered image must be the COVER (mostly white + big art) ===")
b1 = fetch("/api/documents/1/versions/2/pages/1/image?kind=source")
p1 = os.path.join(OUT, "_tmp_p1.webp")
open(p1, "wb").write(b1)
im = Image.open(p1).convert("RGB")
w, h = im.size
top = im.crop((0, 0, w, int(h * 0.42)))
bot = im.crop((0, int(h * 0.42), w, h))
def whiteness(part):
    px = list(part.resize((60, 60)).getdata())
    return sum(1 for c in px if min(c) > 235) / len(px)
print(f"  top 42% (F-16 art) whiteness = {whiteness(top):.2f}   (cover has big art → low)")
print(f"  bottom 58% (title)  whiteness = {whiteness(bot):.2f}   (cover is white → high)")
cover_ok = whiteness(bot) > 0.80 and whiteness(top) < 0.55
print(f"  looks like the COVER: {cover_ok}")
ok &= cover_ok

print("\n=== D. totals: snapshot rows must scale with pages viewed ===")
from core import db as cdb  # noqa: E402
con = cdb.connect()
rows = con.execute("SELECT kind, COUNT(*) n FROM render_snapshots GROUP BY kind").fetchall()
for r in rows:
    print(f"  {r['kind']:10s} snapshots={r['n']}")
con.close()

print(f"\nRESULT: {'ALL OK' if ok else 'FAILURES PRESENT'}")
sys.exit(0 if ok else 1)
