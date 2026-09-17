"""Lead: time /api/diff (incl. include_unchanged) against the production DB."""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"

PATHS = [
    "/api/documents/1/diff?from=1&to=2",
    "/api/documents/1/diff?from=1&to=2",
    "/api/documents/1/diff?from=1&to=2&include_unchanged=1",
    "/api/documents/1/diff?from=2&to=2",
    "/api/documents/1/versions/2/pages/24/markers",
    "/api/documents/1/versions/2/pages/24/markers",
]
for path in PATHS:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(BASE + path, timeout=120) as r:
            body = r.read()
        dt = (time.perf_counter() - t0) * 1000
        try:
            j = json.loads(body)
            counts = j.get("counts")
            nch = len(j.get("changes", []))
            extra = f"counts={counts} changes={nch}"
        except Exception:
            extra = ""
        print(f"{path:60s} {r.status} {dt:9.1f} ms {len(body):9d} B  {extra}")
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        print(f"{path:60s} ERR {dt:9.1f} ms  {type(e).__name__}: {str(e)[:120]}")
