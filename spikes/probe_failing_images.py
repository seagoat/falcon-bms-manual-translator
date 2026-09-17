"""Lead: test the exact page-image URLs that failed to display in the browser."""
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8777"
OUT = "data/derived/preview"
# pages observed NOT loading in the CN pane during diagnosis
PAGES = [40, 42, 43, 44, 120, 124, 300, 302, 303, 304]

print(f"{'page':>5} {'kind':7} {'status':>7} {'ms':>8} {'bytes':>9}  sha")
for n in PAGES:
    for kind in ("source", "cn"):
        url = f"{BASE}/api/documents/1/versions/2/pages/{n}/image?kind={kind}&dpi=110&rev=0"
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                data = r.read()
            ms = (time.perf_counter() - t0) * 1000
            sig = hashlib.sha256(data).hexdigest()[:12]
            ctype = r.headers.get("content-type")
            cache = r.headers.get("X-MT-Cache", "-")
            note = ""
            if len(data) < 5000:
                note = f"  <<< TINY (content-type={ctype})"
            print(f"{n:>5} {kind:7} {r.status:>7} {ms:8.0f} {len(data):9d}  {sig} cache={cache}{note}")
        except urllib.error.HTTPError as e:
            ms = (time.perf_counter() - t0) * 1000
            body = e.read()[:200].decode("utf-8", "replace")
            print(f"{n:>5} {kind:7} {e.code:>7} {ms:8.0f} {'-':>9}  ERR {body}")
        except Exception as e:
            ms = (time.perf_counter() - t0) * 1000
            print(f"{n:>5} {kind:7} {'ERR':>7} {ms:8.0f} {'-':>9}  {type(e).__name__}: {str(e)[:80]}")
