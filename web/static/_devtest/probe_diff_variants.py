"""对比 /api/diff 各种参数的耗时与体积（新代码验收用）。

用法：python web/static/_devtest/probe_diff_variants.py [base_url]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

B = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8903").rstrip("/")
BASE = "/api/documents/1/diff"
VARIANTS = [
    f"{BASE}?from=1&to=2",
    f"{BASE}?from=1&to=2",
    f"{BASE}?from=2&to=2",
    f"{BASE}?from=1&to=2&include_unchanged=1",
    f"{BASE}?from=1&to=2&page=21",
]


def get(p):
    t0 = time.time()
    try:
        with urllib.request.urlopen(B + p, timeout=200) as r:
            blob = r.read()
            return r.status, time.time() - t0, len(blob), json.loads(blob.decode("utf-8"))
    except Exception as exc:
        return 0, time.time() - t0, 0, str(exc)


print(f"== /api/diff variants @ {B} ==")
for p in VARIANTS:
    st, dt, n, d = get(p)
    info = ""
    if isinstance(d, dict):
        info = (f"changes={len(d.get('changes', []))} omitted={d.get('unchanged_omitted')} "
                f"counts={json.dumps(d.get('counts'), ensure_ascii=False)[:120]}")
    print(f"  {p.replace(BASE, ''):46s} HTTP {st} {dt*1000:7.0f} ms {n/1024:8.1f} KB  {info}")
