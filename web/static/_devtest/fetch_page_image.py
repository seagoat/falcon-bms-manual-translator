"""抓取页面图到本地以便目视检查（含缓存头）。

用法：python web/static/_devtest/fetch_page_image.py <base_url> <doc> <vid> <page> <kind> <out.webp>
"""
from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

B = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8901").rstrip("/")
DOC, VID, PAGE, KIND = (sys.argv[2:6] if len(sys.argv) > 5 else ["1", "2", "21", "cn"])
OUT = Path(sys.argv[6] if len(sys.argv) > 6 else f"data/derived/preview/_page{PAGE}_{KIND}.webp")

t0 = time.time()
url = f"{B}/api/documents/{DOC}/versions/{VID}/pages/{PAGE}/image?kind={KIND}&dpi=110"
with urllib.request.urlopen(url, timeout=300) as r:
    blob = r.read()
    hdr = dict(r.headers)
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_bytes(blob)
print(f"GET {url}")
print(f"  -> HTTP 200 {len(blob)} bytes in {time.time()-t0:.2f}s  "
      f"cache={hdr.get('X-MT-Cache')} fallback={hdr.get('X-MT-Fallback')}")
print(f"  saved {OUT}")
